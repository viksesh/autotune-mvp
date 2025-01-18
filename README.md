# autotune-mvp
Auto Tune Kube Resources


# AutoTune MVP - Container-based CronJob + Test Workloads

This repository shows how to:

1. **Build** a Docker image with `aggregator.py` (AutoTune logic).  
2. **Deploy** a Helm chart that runs a **CronJob** referencing that image, plus multiple test workloads.

# Partial Tuning Labels
Each Deployment has labels:

container-efficiency.io/autotune: "enabled"
container-efficiency.io/tune-cpu: "enabled"/"disabled"
container-efficiency.io/tune-memory: "enabled"/"disabled"
container-efficiency.io/tune-limits: "enabled"/"disabled"


