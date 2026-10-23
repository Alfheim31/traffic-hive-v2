"""MAPPO for cooperative corridor assignment, in NumPy.

Multi-Agent PPO with centralised training and decentralised execution:

  actor   pi_theta(a_i | o_i)        one network shared by all 16 OD-pair
                                     agents; the one-hot agent id in o_i
                                     lets it specialise per OD pair
  critic  V_phi(o_i, s)              sees the global state s during
                                     training only

Advantages use GAE(gamma, lambda) per agent. The actor minimises the PPO
clipped surrogate

  L_pi = -E[ min(rho A, clip(rho, 1 - c, 1 + c) A) ] - beta_H E[H(pi(.|o))]
  rho  = pi_theta(a|o) / pi_theta_old(a|o)

and the critic the squared error to the GAE return. Networks are small
(two tanh hidden layers), so NumPy with hand-written backpropagation is
fast enough on a laptop CPU and keeps every step of the learning rule
inspectable. `python -m pipeline.marl --gradcheck` verifies the gradients
against finite differences.

    python -m pipeline.marl train --iterations 60          # trains, saves every iteration
    python -m pipeline.marl train --iterations 60 --resume # continues from the last checkpoint
    python -m pipeline.marl plot                           # learning curve figure
    python -m pipeline.marl --gradcheck
    python -m pipeline.marl train --mode residual ...       # agents adjust the Hive rule (see marl_env)

The trained actor is exported to models/marl_policy.npz, which the
`marl_corridor` scenario loads to run in SUMO without any training code. In
residual mode it goes to models/marl_residual_policy.npz, run by the
`hive_marl_corridor` scenario.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

CHECKPOINT = Path("data/models/marl_checkpoint.npz")
POLICY = Path("models/marl_policy.npz")
LOG = Path("data/models/marl_training_log.jsonl")
OBSTACLES = "examples/obstacles_espana_demo.json"


def paths(mode: str) -> tuple[Path, Path, Path]:
    """Checkpoint, exported policy and log for a mode ("split" or "residual")."""
    if mode == "residual":
        return (Path("data/models/marl_residual_checkpoint.npz"), Path("models/marl_residual_policy.npz"),
                Path("data/models/marl_residual_training_log.jsonl"))
    return CHECKPOINT, POLICY, LOG


# --------------------------------------------------------------------------
# Networks
# --------------------------------------------------------------------------

class MLP:
    """Fully connected network with tanh hidden layers and a linear output."""

    def __init__(self, sizes: list[int], rng: np.random.Generator, out_gain: float = 1.0) -> None:
        self.params: list[np.ndarray] = []
        for i, (a, b) in enumerate(zip(sizes[:-1], sizes[1:])):
            gain = out_gain if i == len(sizes) - 2 else math.sqrt(2)
            w = rng.standard_normal((a, b)) * gain / math.sqrt(a)   # scaled init, close to orthogonal
            self.params += [w, np.zeros(b)]

    def forward(self, x: np.ndarray) -> np.ndarray:
        self._cache = [x]
        h = x
        n = len(self.params) // 2
        for i in range(n):
            z = h @ self.params[2 * i] + self.params[2 * i + 1]
            h = np.tanh(z) if i < n - 1 else z
            self._cache.append(h)
        return h

    def backward(self, grad_out: np.ndarray) -> list[np.ndarray]:
        grads = [None] * len(self.params)
        n = len(self.params) // 2
        g = grad_out
        for i in reversed(range(n)):
            h_in = self._cache[i]
            grads[2 * i] = h_in.T @ g
            grads[2 * i + 1] = g.sum(0)
            if i > 0:
                g = (g @ self.params[2 * i].T) * (1.0 - self._cache[i] ** 2)
        return grads


class Adam:
    def __init__(self, params: list[np.ndarray], lr: float) -> None:
        self.lr, self.t = lr, 0
        self.m = [np.zeros_like(p) for p in params]
        self.v = [np.zeros_like(p) for p in params]

    def step(self, params: list[np.ndarray], grads: list[np.ndarray], max_norm: float = 0.5) -> float:
        norm = math.sqrt(sum(float((g * g).sum()) for g in grads))
        scale = min(1.0, max_norm / (norm + 1e-8))
        self.t += 1
        for p, g, m, v in zip(params, grads, self.m, self.v):
            g = g * scale
            m *= 0.9; m += 0.1 * g
            v *= 0.999; v += 0.001 * g * g
            mh, vh = m / (1 - 0.9 ** self.t), v / (1 - 0.999 ** self.t)
            p -= self.lr * mh / (np.sqrt(vh) + 1e-8)
        return norm


class RunningNorm:
    """Running mean and variance for input normalisation."""

    def __init__(self, dim: int) -> None:
        self.mean, self.var, self.count = np.zeros(dim), np.ones(dim), 1e-4

    def update(self, x: np.ndarray) -> None:
        bm, bv, bc = x.mean(0), x.var(0), x.shape[0]
        delta, tot = bm - self.mean, self.count + bc
        self.mean = self.mean + delta * bc / tot
        self.var = (self.var * self.count + bv * bc + delta ** 2 * self.count * bc / tot) / tot
        self.count = tot

    def __call__(self, x: np.ndarray) -> np.ndarray:
        return np.clip((x - self.mean) / np.sqrt(self.var + 1e-8), -5, 5)


def softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(-1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(-1, keepdims=True)


# --------------------------------------------------------------------------
# Policy (actor only), used for rollouts and in SUMO evaluation
# --------------------------------------------------------------------------

class Policy:
    def __init__(self, actor_params: list[np.ndarray], obs_mean: np.ndarray, obs_var: np.ndarray, actions: np.ndarray | None = None) -> None:
        self.p, self.mean, self.var, self.actions = actor_params, obs_mean, obs_var, actions

    def logits(self, obs: np.ndarray) -> np.ndarray:
        h = np.clip((obs - self.mean) / np.sqrt(self.var + 1e-8), -5, 5)
        n = len(self.p) // 2
        for i in range(n):
            h = h @ self.p[2 * i] + self.p[2 * i + 1]
            if i < n - 1:
                h = np.tanh(h)
        return h

    def act(self, obs: np.ndarray, rng: np.random.Generator, greedy: bool = False) -> tuple[np.ndarray, np.ndarray]:
        probs = softmax(self.logits(obs))
        if greedy:
            a = probs.argmax(1)
        else:
            a = np.array([rng.choice(len(p), p=p) for p in probs])
        return a, np.log(probs[np.arange(len(a)), a] + 1e-12)

    def save(self, path: Path, meta: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, *self.p, obs_mean=self.mean, obs_var=self.var, actions=self.actions, meta=json.dumps(meta))

    @classmethod
    def load(cls, path: Path) -> "Policy":
        z = np.load(path, allow_pickle=False)
        params = [z[f"arr_{i}"] for i in range(len([k for k in z.files if k.startswith("arr_")]))]
        pol = cls(params, z["obs_mean"], z["obs_var"], z["actions"])
        pol.meta = json.loads(str(z["meta"]))
        return pol


# --------------------------------------------------------------------------
# Rollouts
# --------------------------------------------------------------------------

@dataclass
class TrainConfig:
    iterations: int = 60
    episodes_per_iter: int = 4
    workers: int = 2
    vehicles_min: int = 280
    vehicles_max: int = 420
    horizon: int = 300
    tail: int = 600
    stranded_s: float = 600.0
    obstacle_prob: float = 0.5
    gamma: float = 0.99
    lam: float = 0.95
    clip: float = 0.2
    actor_lr: float = 3e-4
    critic_lr: float = 1e-3
    epochs: int = 6
    minibatch: int = 512
    entropy_start: float = 0.02
    entropy_end: float = 0.003
    alpha: float = 0.5
    epsilon: float = 0.25
    fairness_weight: float = 0.5
    seed: int = 7
    mode: str = "split"           # "residual": agents adjust the Hive rule instead of replacing it


def run_episode(args: tuple) -> dict:
    """One SUMO episode with the current policy (runs in a worker process)."""
    actor_params, obs_mean, obs_var, env_kw, rng_seed, greedy = args
    os.environ.setdefault("SUMO_HOME", os.environ.get("SUMO_HOME", ""))
    from pipeline.marl_env import CorridorMARLEnv, EnvConfig
    rng = np.random.default_rng(rng_seed)
    env = CorridorMARLEnv(EnvConfig(**env_kw))
    pol = Policy(actor_params, obs_mean, obs_var)
    obs, state = env.reset()
    buf = {k: [] for k in ("obs", "state", "act", "logp", "rew", "done")}
    ret = np.zeros(env.n_agents)
    while True:
        a, lp = pol.act(obs, rng, greedy)
        nobs, nstate, r, done, info = env.step(a)
        for k, v in (("obs", obs), ("state", state), ("act", a), ("logp", lp), ("rew", r), ("done", done)):
            buf[k].append(v)
        ret += r
        obs, state = nobs, nstate
        if done:
            break
    summary = env.assignment_summary()
    stranded = info["in_network"]
    env.close()
    out = {k: np.array(v) for k, v in buf.items()}
    out.update({"return": float(ret.mean()), "stranded": stranded, "vehicles": env_kw["vehicles"],
                "obstacles": bool(env_kw.get("obstacles")), "share": summary["share"], "entropy": summary["spread_entropy"],
                "epochs": len(buf["rew"])})
    return out


# --------------------------------------------------------------------------
# Trainer
# --------------------------------------------------------------------------

class MAPPO:
    def __init__(self, obs_dim: int, state_dim: int, n_actions: int, cfg: TrainConfig) -> None:
        rng = np.random.default_rng(cfg.seed)
        self.cfg = cfg
        self.actor = MLP([obs_dim, 64, 64, n_actions], rng, out_gain=0.01)
        self.critic = MLP([obs_dim + state_dim, 128, 64, 1], rng, out_gain=1.0)
        self.aopt = Adam(self.actor.params, cfg.actor_lr)
        self.copt = Adam(self.critic.params, cfg.critic_lr)
        self.obs_norm, self.state_norm = RunningNorm(obs_dim), RunningNorm(state_dim)
        self.ret_std = 1.0
        self.iteration = 0

    # -- persistence ----------------------------------------------------------
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        arrays = {}
        for name, net, opt in (("a", self.actor, self.aopt), ("c", self.critic, self.copt)):
            for i, (p, m, v) in enumerate(zip(net.params, opt.m, opt.v)):
                arrays[f"{name}p{i}"], arrays[f"{name}m{i}"], arrays[f"{name}v{i}"] = p, m, v
        for name, rn in (("on", self.obs_norm), ("sn", self.state_norm)):
            arrays[f"{name}_mean"], arrays[f"{name}_var"], arrays[f"{name}_count"] = rn.mean, rn.var, np.array(rn.count)
        arrays["scalars"] = np.array([self.ret_std, self.iteration, self.aopt.t, self.copt.t], dtype=np.float64)
        np.savez(path, **arrays)

    def load(self, path: Path) -> None:
        z = np.load(path)
        for name, net, opt in (("a", self.actor, self.aopt), ("c", self.critic, self.copt)):
            for i in range(len(net.params)):
                net.params[i][...] = z[f"{name}p{i}"]; opt.m[i][...] = z[f"{name}m{i}"]; opt.v[i][...] = z[f"{name}v{i}"]
        for name, rn in (("on", self.obs_norm), ("sn", self.state_norm)):
            rn.mean, rn.var, rn.count = z[f"{name}_mean"], z[f"{name}_var"], float(z[f"{name}_count"])
        self.ret_std, it, at, ct = z["scalars"]
        self.iteration, self.aopt.t, self.copt.t = int(it), int(at), int(ct)

    # -- update -----------------------------------------------------------------
    def _values(self, obs: np.ndarray, state: np.ndarray) -> np.ndarray:
        x = np.concatenate([self.obs_norm(obs), self.state_norm(state)], -1)
        return self.critic.forward(x)[:, 0]

    def update(self, episodes: list[dict]) -> dict:
        cfg = self.cfg
        # normalisers first, so every sample in this batch is normalised the same way
        all_obs = np.concatenate([e["obs"].reshape(-1, e["obs"].shape[-1]) for e in episodes])
        all_state = np.concatenate([e["state"] for e in episodes])
        self.obs_norm.update(all_obs); self.state_norm.update(all_state)
        rets = np.concatenate([e["rew"].sum(0) for e in episodes])
        self.ret_std = 0.9 * self.ret_std + 0.1 * float(np.std(rets) + 1e-6) if self.iteration else float(np.std(rets) + 1e-6)

        O, S, A, LP, ADV, RET = [], [], [], [], [], []
        for e in episodes:
            T, N = e["rew"].shape
            obs = e["obs"]; state = np.repeat(e["state"][:, None, :], N, 1)
            v = self._values(obs.reshape(T * N, -1), state.reshape(T * N, -1)).reshape(T, N)
            r = e["rew"] / self.ret_std
            adv = np.zeros((T, N)); last = np.zeros(N)
            for t in reversed(range(T)):
                nv = v[t + 1] if t + 1 < T else np.zeros(N)       # episode ends at T: terminal value 0
                delta = r[t] + cfg.gamma * nv - v[t]
                last = delta + cfg.gamma * cfg.lam * last
                adv[t] = last
            O.append(obs.reshape(T * N, -1)); S.append(state.reshape(T * N, -1)); A.append(e["act"].ravel())
            LP.append(e["logp"].ravel()); ADV.append(adv.ravel()); RET.append((adv + v).ravel())
        O, S, A, LP, ADV, RET = map(np.concatenate, (O, S, A, LP, ADV, RET))
        ADV = (ADV - ADV.mean()) / (ADV.std() + 1e-8)
        On, Sn = self.obs_norm(O), self.state_norm(S)
        frac = min(1.0, self.iteration / max(1, cfg.iterations - 1))
        ent_coef = cfg.entropy_start + (cfg.entropy_end - cfg.entropy_start) * frac

        rng = np.random.default_rng(cfg.seed + self.iteration)
        n = len(A); stats = {"pi_loss": [], "v_loss": [], "entropy": [], "kl": [], "clipfrac": []}
        for _ in range(cfg.epochs):
            idx = rng.permutation(n)
            for s in range(0, n, cfg.minibatch):
                b = idx[s:s + cfg.minibatch]; m = len(b)
                # actor
                logits = self.actor.forward(On[b]); p = softmax(logits); logp_all = np.log(p + 1e-12)
                lp = logp_all[np.arange(m), A[b]]
                ratio = np.exp(lp - LP[b]); adv = ADV[b]
                unclipped = ratio * adv; clipped = np.clip(ratio, 1 - cfg.clip, 1 + cfg.clip) * adv
                use_unclipped = unclipped <= clipped
                ent = -(p * logp_all).sum(1)
                dlogp = np.where(use_unclipped, -adv * ratio, 0.0) / m
                onehot = np.zeros_like(p); onehot[np.arange(m), A[b]] = 1.0
                g_logits = dlogp[:, None] * (onehot - p)
                g_logits += ent_coef * p * (logp_all + ent[:, None]) / m      # d(-c*H)/dz
                self.aopt.step(self.actor.params, self.actor.backward(g_logits))
                # critic
                x = np.concatenate([On[b], Sn[b]], 1)
                v = self.critic.forward(x)[:, 0]
                self.copt.step(self.critic.params, self.critic.backward(((v - RET[b]) / m)[:, None]), max_norm=1.0)
                stats["pi_loss"].append(float(-np.minimum(unclipped, clipped).mean()))
                stats["v_loss"].append(float(0.5 * ((v - RET[b]) ** 2).mean()))
                stats["entropy"].append(float(ent.mean()))
                stats["kl"].append(float((LP[b] - lp).mean()))
                stats["clipfrac"].append(float((np.abs(ratio - 1) > cfg.clip).mean()))
        self.iteration += 1
        return {k: round(float(np.mean(v)), 4) for k, v in stats.items()} | {"entropy_coef": round(ent_coef, 4)}

    def policy(self, actions: np.ndarray | None = None) -> Policy:
        return Policy([p.copy() for p in self.actor.params], self.obs_norm.mean.copy(), self.obs_norm.var.copy(), actions)


def episode_settings(cfg: TrainConfig, rng: np.random.Generator) -> dict:
    return {"vehicles": int(rng.integers(cfg.vehicles_min, cfg.vehicles_max + 1)), "horizon": cfg.horizon,
            "tail": cfg.tail, "seed": int(rng.integers(1, 10**6)),
            "obstacles": OBSTACLES if rng.random() < cfg.obstacle_prob else None,
            "alpha": cfg.alpha, "epsilon": cfg.epsilon, "fairness_weight": cfg.fairness_weight,
            "stranded_s": cfg.stranded_s, "mode": cfg.mode}


def train(cfg: TrainConfig, resume: bool) -> None:
    from pipeline.marl_env import CorridorCore
    checkpoint, policy_path, log = paths(cfg.mode)
    core = CorridorCore(Path("data/net/corridors.json"), cfg.horizon, mode=cfg.mode)
    algo = MAPPO(core.obs_dim, core.state_dim, core.n_actions, cfg)
    if resume and checkpoint.exists():
        algo.load(checkpoint)
        print(f"resumed at iteration {algo.iteration}")
    elif cfg.mode == "residual":
        # start close to the rule itself: "no change" (action 0) gets about
        # 40 % of the probability, the twelve adjustments share the rest
        algo.actor.params[-1][0] += 2.0
    rng = np.random.default_rng(cfg.seed + 1000 * algo.iteration)
    log.parent.mkdir(parents=True, exist_ok=True)
    with ProcessPoolExecutor(cfg.workers) as pool:
        while algo.iteration < cfg.iterations:
            t0 = time.time()
            pol = algo.policy()
            jobs = [(pol.p, pol.mean, pol.var, episode_settings(cfg, rng), int(rng.integers(1, 10**9)), False)
                    for _ in range(cfg.episodes_per_iter)]
            eps = list(pool.map(run_episode, jobs))
            stats = algo.update(eps)
            rec = {"iteration": algo.iteration, "wall_s": round(time.time() - t0, 1),
                   "return": round(float(np.mean([e["return"] for e in eps])), 3),
                   "stranded_per_100": round(float(np.mean([e["stranded"] / e["vehicles"] * 100 for e in eps])), 2),
                   "share": np.mean([e["share"] for e in eps], 0).round(3).tolist(),
                   "split_entropy": round(float(np.mean([e["entropy"] for e in eps])), 3), **stats}
            with log.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
            print(json.dumps(rec), flush=True)
            algo.save(checkpoint)
            algo.policy(core.actions).save(policy_path, {"iteration": algo.iteration, "config": asdict(cfg),
                                                         "obs_dim": core.obs_dim, "n_actions": core.n_actions,
                                                         "agents": core.n, "mode": cfg.mode})


# --------------------------------------------------------------------------
# Gradient check
# --------------------------------------------------------------------------

def gradcheck() -> None:
    rng = np.random.default_rng(0)
    net = MLP([6, 8, 5, 4], rng, out_gain=1.0)
    x = rng.standard_normal((7, 6)); a = rng.integers(0, 4, 7); adv = rng.standard_normal(7)
    old = rng.standard_normal(7) * 0.1 - 1.4; c, ent_coef = 0.2, 0.05

    def loss() -> float:
        z = net.forward(x); p = softmax(z); lpa = np.log(p + 1e-12)
        lp = lpa[np.arange(7), a]; r = np.exp(lp - old)
        surr = np.minimum(r * adv, np.clip(r, 1 - c, 1 + c) * adv)
        return float(-surr.mean() - ent_coef * (-(p * lpa).sum(1)).mean())

    z = net.forward(x); p = softmax(z); lpa = np.log(p + 1e-12); lp = lpa[np.arange(7), a]
    r = np.exp(lp - old); un = r * adv; cl = np.clip(r, 1 - c, 1 + c) * adv
    ent = -(p * lpa).sum(1)
    onehot = np.zeros_like(p); onehot[np.arange(7), a] = 1
    g = np.where(un <= cl, -adv * r, 0.0)[:, None] / 7 * (onehot - p) + ent_coef * p * (lpa + ent[:, None]) / 7
    grads = net.backward(g)
    worst = 0.0
    for pi, param in enumerate(net.params):
        for _ in range(6):
            idx = tuple(rng.integers(0, s) for s in param.shape)
            old_v = param[idx]
            param[idx] = old_v + 1e-6; lp_ = loss()
            param[idx] = old_v - 1e-6; lm = loss()
            param[idx] = old_v
            num = (lp_ - lm) / 2e-6
            worst = max(worst, abs(num - grads[pi][idx]) / max(1e-6, abs(num) + abs(grads[pi][idx])))
    print(f"actor gradient check: worst relative error {worst:.2e}")
    crit = MLP([6, 8, 1], rng); y = rng.standard_normal(7)
    v = crit.forward(x)[:, 0]; cg = crit.backward(((v - y) / 7)[:, None]); w2 = 0.0
    for pi, param in enumerate(crit.params):
        for _ in range(6):
            idx = tuple(rng.integers(0, s) for s in param.shape); o = param[idx]
            param[idx] = o + 1e-6; l1 = 0.5 * ((crit.forward(x)[:, 0] - y) ** 2).mean()
            param[idx] = o - 1e-6; l2 = 0.5 * ((crit.forward(x)[:, 0] - y) ** 2).mean()
            param[idx] = o; num = (l1 - l2) / 2e-6
            w2 = max(w2, abs(num - cg[pi][idx]) / max(1e-6, abs(num) + abs(cg[pi][idx])))
    print(f"critic gradient check: worst relative error {w2:.2e}")


def plot(out: Path = Path("figures/fig7_marl_learning_curve"), mode: str = "split") -> None:
    """Learning curve: episode return per iteration with reference policies."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    # Phase 1 (if kept) used 300 s tails and a 300 s stranded cost; phase 2
    # resumed from it with 600 s for both. Returns are on comparable scales
    # but not identical, so the switch is marked.
    log = paths(mode)[2]
    phase1 = log.with_name("marl_training_log_phase1.jsonl")
    logs = [p for p in (phase1, log) if p.exists()]
    recs = [json.loads(l) for p in logs for l in p.read_text().splitlines() if l.strip()]
    it = np.array([r["iteration"] for r in recs]); ret = np.array([r["return"] for r in recs])
    k = max(1, min(9, len(ret) // 5))
    smooth = np.convolve(ret, np.ones(k) / k, mode="valid")
    fig, ax = plt.subplots(figsize=(7.2, 3.8), dpi=150)
    ax.plot(it, ret, color="#1D6E56", alpha=0.25, lw=1, label="MARL, per iteration")
    ax.plot(it[k - 1:], smooth, color="#1D6E56", lw=2, label=f"MARL, {k}-iteration mean")
    split_at = None
    if len(logs) == 2:
        split_at = sum(1 for l in phase1.read_text().splitlines() if l.strip())
        ax.axvline(split_at + 0.5, color="#475569", lw=1, ls=(0, (2, 2)))
    ref = Path("data/models/baselines.json")
    if ref.exists() and mode == "split":
        # reference lines: fixed policies scored on four fixed held-out episodes
        styles = {"hive_rule": ("#B4623A", "-", "Traffic Hive rule"), "fastest_only": ("#5E6353", "--", "Everyone on fastest corridor"),
                  "even_split": ("#8C9080", ":", "Fixed even split")}
        for key, val in json.loads(ref.read_text()).items():
            c, ls, lab = styles.get(key, ("#555", "-.", key))
            # reference scores apply to phase-1 episodes only, so stop the line there
            ax.hlines(val, 0, (split_at + 0.5) if split_at else it.max(), color=c, lw=1.4, linestyles=ls,
                      label=f"{lab} (phase-1 episodes)")
    if split_at:
        top = ax.get_ylim()[1]
        ax.text(split_at + 2, top - 0.6, "phase 2: longer episodes,\nlarger stranded cost", fontsize=7.5,
                color="#475569", va="top")
    ax.set_xlabel("Training iteration (4 SUMO episodes each)")
    ax.set_ylabel("Mean episode return (higher is better)")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", alpha=0.25)
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    fig.tight_layout()
    out.parent.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(f"{out}.{ext}")
    print(f"written {out}.png and .pdf")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["train", "plot"], default=None)
    parser.add_argument("--gradcheck", action="store_true")
    parser.add_argument("--resume", action="store_true")
    for f, v in asdict(TrainConfig()).items():
        parser.add_argument(f"--{f.replace('_', '-')}", type=type(v), default=v)
    args = parser.parse_args()
    if args.gradcheck:
        gradcheck(); return
    if args.command == "plot":
        if args.mode == "residual":
            plot(Path("figures/fig8_marl_residual_learning_curve"), mode="residual")
        else:
            plot()
        return
    if args.command == "train":
        cfg = TrainConfig(**{f: getattr(args, f) for f in asdict(TrainConfig())})
        train(cfg, args.resume)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
