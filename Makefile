VEHICLES ?= 1000
HORIZON  ?= 900
SEED     ?= 42

net:
	python -m pipeline.build_network --osm data/raw/map.osm

audit:
	python -m pipeline.build_network --audit-only

runs:
	python -m pipeline.run_scenarios --all --vehicles $(VEHICLES) --horizon $(HORIZON) --seed $(SEED)

pack:
	python -m pipeline.pack --scenarios fixed actuated ue hive --baseline ue

all: net runs pack

clean:
	rm -rf data/out app/assets/sim/*.bin

calibrate:
	python -m pipeline.calibrate --sweep 100 200 400 700 1000 1500 --horizon 600

figures:
	python -m pipeline.figures --all

theory:
	python -m pipeline.theory --demand 10 --report
