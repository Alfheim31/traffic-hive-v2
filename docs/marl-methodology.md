# Learning-based corridor assignment (MARL)

*Draft for Chapter 3. Section numbers to be fitted to the manuscript.*

## Overview

The heuristic Traffic Hive rule assigns each departing vehicle to a corridor by a fixed formula: a softmax over the marginal cost of each corridor, computed from measured travel time, with a fairness tolerance ε. The learning-based variant keeps the same corridors, decision epoch and fairness tolerance, but replaces the formula with a policy learned through multi-agent reinforcement learning (MARL). Each origin–destination (OD) pair in the corridor set is one agent, and all agents are trained together in the SUMO model of the España corridor using Multi-Agent Proximal Policy Optimization (MAPPO) with centralised training and decentralised execution.

## Agents and decision epoch

Let 𝒢 = {1, …, 16} be the OD pairs discovered by the corridor procedure, each with K = 3 corridors. Each OD pair *i* is an agent. Decisions are taken every Δ = 10 s of simulated time. At epoch *t*, agent *i* chooses how the vehicles departing from its origin during the next epoch are split across its corridors.

## Observation

Each agent observes only its own corridors and a three-value summary of the network. The observation vector o_i,t has 30 components:

| Component | Definition | Size |
|---|---|---|
| Delay ratio of corridor *k* | T_ik / T⁰_ik − 1, where T_ik is the measured travel time (sum of SUMO per-edge travel-time estimates, exponentially smoothed with weight 0.4) and T⁰_ik the free-flow time; capped at 4 | 3 |
| Occupancy of corridor *k* | N_ik / C_ik, vehicles on the corridor over its pipe-model capacity; capped at 3 | 3 |
| Trend of corridor *k* | change in delay ratio since the previous epoch, clipped to ±2 | 3 |
| Own load | agent's vehicles in the network / 50, agent's departures in the last epoch / 10 | 2 |
| Network summary | mean delay ratio over all corridors, vehicles in the network / 500, t / horizon | 3 |
| Agent identity | one-hot index of the OD pair | 16 |

Observations are standardised with running means and variances collected during training.

## Action

The action a_i,t selects one split p ∈ Δ^K from a grid with step 0.25 on the simplex, giving 15 splits for K = 3 (for example (1, 0, 0), (0.5, 0.25, 0.25), (0, 0.5, 0.5)). Each vehicle departing from origin *i* during the epoch is assigned to corridor *k* with probability p_k.

## Reward

Let D_i,t be the vehicle-seconds accrued during epoch *t* by agent *i*'s vehicles: those in the network plus those whose scheduled departure has passed but which SUMO could not yet insert. The reward is

r_i,t = −[α D_i,t + (1 − α) D̄_t] / S − λ_f Σ_k p_k max(0, T_ik / min_j T_ij − (1 + ε))

where D̄_t is the mean of D_j,t over all agents, α = 0.5, S = 200 vehicle-seconds, λ_f = 0.5 and ε = 0.25.

The first term is cooperative. Summed over agents and epochs, Σ_i Σ_t D_i,t is the total system travel time, which is the objective of the system-optimal assignment the thesis targets. Mixing each agent's own delay with the network mean (α = 0.5) gives every agent a share of the network outcome, which discourages relieving its own corridors by pushing traffic onto others. Counting vehicles that are waiting to enter the network prevents an agent from lowering its cost by blocking insertion.

The second term is the fairness tolerance of the heuristic rule, applied to expected travel time: probability placed on a corridor expected to take more than (1 + ε) times the fastest one is penalised in proportion to the excess.

When an episode is cut off, each vehicle still in the network adds S_str seconds to its agent's cost (300 s in training phase 1, 600 s in phase 2, about one full trip on this network), so a policy cannot reduce its cost by leaving traffic trapped.

## Learning algorithm

All agents share one actor π_θ(a | o_i), a network with two hidden layers of 64 tanh units and a 15-way softmax output. The one-hot agent identity lets the shared policy behave differently per OD pair while training from the pooled experience of all 16. A centralised critic V_φ(o_i, s) with hidden layers of 128 and 64 tanh units receives, during training only, the global state s: the delay ratio and occupancy of all 48 corridors plus the network summary (99 values). At execution each agent uses only o_i.

Advantages are computed per agent with generalised advantage estimation (γ = 0.99, λ = 0.95) on rewards scaled by a running standard deviation of episode returns, and normalised per batch. The actor minimises the clipped surrogate

L(θ) = −E[min(ρ Â, clip(ρ, 1 − 0.2, 1 + 0.2) Â)] − β_H E[H(π_θ(· | o))],  ρ = π_θ(a | o) / π_θ_old(a | o)

with entropy weight β_H decayed linearly from 0.01 to 0.001 over the planned iterations. The critic minimises the squared error to the GAE return. Both are trained with Adam (critic learning rate 1×10⁻³; actor 1×10⁻³ in phase 1 and 5×10⁻⁴ in phase 2), 8 epochs per batch, minibatches of 512 samples and gradient-norm clipping (0.5 actor, 1.0 critic). The networks and their gradients are implemented in NumPy; the gradients were verified against finite differences (maximum relative error below 10⁻⁶).

## Training episodes

Each training iteration runs four SUMO episodes in parallel with the current policy, then performs one MAPPO update. To prevent the policy from fitting a single scenario, every episode samples its own conditions: 280–420 vehicles over a 300 s demand window (the same arrival rate as 1,000 vehicles over 900 s), a fresh random seed for demand and driver behaviour, and, with probability 0.5, the twelve España obstacles.

Training ran in two phases. In phase 1 (iterations 1–150) episodes ended 300 s after the demand window closed, with S_str = 300 s. Because trips on this network take about eight minutes, roughly 40% of vehicles were still travelling at the cut-off, so the reward under-counted the cost of slow or congested corridors. Phase 2 (iterations 151–188) resumed from the phase-1 policy with a 600 s tail and S_str = 600 s, which lowered the share of vehicles still travelling at the cut-off to about 6%. Training stopped when the 10-iteration mean return had not improved for 38 iterations.

## Evaluation

The trained policy is evaluated in the same pipeline as the other rules, as the `marl_corridor` scenario, with greedy actions (the most probable split). It is compared with selfish user equilibrium (`ue_corridor`) and the heuristic Traffic Hive rule (`hive_corridor`) on identical demand for five held-out seeds, at 1,000 vehicles over 900 s, with and without obstacles, using `pipeline.evaluate`. The seeds and the 900 s demand window are not seen during training.

Each evaluation run continues for 1,800 s after the demand window closes, so that nearly every trip completes. The primary measure is total vehicle-hours: the time of all vehicles in the network or waiting to enter it, summed over the run, whether or not they finished. Per-trip averages (time loss, waiting) are reported as secondary measures, because they are taken over completed trips only and favour a rule that leaves more vehicles stranded.
