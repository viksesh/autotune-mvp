#!/usr/bin/env python3

import os
import datetime
import logging
from prometheus_api_client import PrometheusConnect
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

logging.basicConfig(level=logging.INFO)

# ENV Variables
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus-server.monitoring.svc.cluster.local")
TENANT_NAMESPACES_STR = os.getenv("TENANT_NAMESPACES", "")
DEFAULT_LOOKBACK_MINUTES = int(os.getenv("DEFAULT_LOOKBACK_MINUTES", "10080"))  # 7 days
DEFAULT_TARGET_PERCENTILE = float(os.getenv("DEFAULT_TARGET_PERCENTILE", "95"))
AGGREGATOR_MAX_LOOKBACK_MINUTES = int(os.getenv("AGGREGATOR_MAX_LOOKBACK_MINUTES", "20160"))  # 14 days
PROMETHEUS_STEP = os.getenv("PROMETHEUS_STEP", "30m")  # 30-minute step

MIN_CPU_REQUEST = 50
MAX_CPU_LIMIT  = 4000
MIN_MEM_REQUEST = 128
MAX_MEM_LIMIT   = 8192

# Label keys
AUTOTUNE_LABEL   = "container-efficiency.io/autotune"        
LOOKBACK_LABEL   = "container-efficiency.io/lookback-minutes" 
PERCENTILE_LABEL = "container-efficiency.io/target-percentile"
TUNE_CPU_LABEL   = "container-efficiency.io/tune-cpu"
TUNE_MEM_LABEL   = "container-efficiency.io/tune-memory"
TUNE_LIMITS_LABEL= "container-efficiency.io/tune-limits"

def build_cpu_query(ns_regex):
    """
    5-min subwindow inside each 30-min block => capturing CPU spikes
    """
    return f'''
max_over_time(
  rate(container_cpu_usage_seconds_total{{namespace=~"{ns_regex}", container!="", pod!=""}}[5m])[30m:5m]
)
'''.strip()

def build_mem_query(ns_regex):
    """
    Peak memory usage in each 30-min block
    """
    return f'''
max_over_time(
  container_memory_working_set_bytes{{namespace=~"{ns_regex}", container!="", pod!=""}}[30m]
)
'''.strip()

def main():
    logging.info("Starting AutoTune aggregator...")

    tenant_ns_list = [x.strip() for x in TENANT_NAMESPACES_STR.split(",") if x.strip()]
    if tenant_ns_list:
        ns_regex = "(" + "|".join(tenant_ns_list) + ")"
    else:
        ns_regex = ".*"

    cpu_query = build_cpu_query(ns_regex)
    mem_query = build_mem_query(ns_regex)

    # Connect to Prometheus
    prom = PrometheusConnect(url=PROMETHEUS_URL, disable_ssl=True)

    # K8s client
    try:
        config.load_incluster_config()
    except:
        config.load_kube_config()
    apps_api = client.AppsV1Api()

    end_time = datetime.datetime.utcnow()
    start_time = end_time - datetime.timedelta(minutes=AGGREGATOR_MAX_LOOKBACK_MINUTES)

    logging.info(f"CPU Query: {cpu_query}")
    logging.info(f"MEM Query: {mem_query}")
    logging.info(f"Range: {start_time} -> {end_time}, step={PROMETHEUS_STEP}")

    try:
        cpu_data = prom.custom_query_range(
            query=cpu_query,
            start_time=start_time,
            end_time=end_time,
            step=PROMETHEUS_STEP
        )
        mem_data = prom.custom_query_range(
            query=mem_query,
            start_time=start_time,
            end_time=end_time,
            step=PROMETHEUS_STEP
        )
    except Exception as e:
        logging.error(f"Prometheus query failed: {e}")
        return

    usage_map = build_usage_map(cpu_data, mem_data)

    # List Deployments
    try:
        all_deps = apps_api.list_deployment_for_all_namespaces().items
    except ApiException as ex:
        logging.error(f"Failed to list deployments: {ex}")
        return

    # Patch logic
    for dep in all_deps:
        ns = dep.metadata.namespace
        if tenant_ns_list and (ns not in tenant_ns_list):
            continue

        if not is_auto_tune_enabled(dep):
            continue

        # Label-based overrides
        lookback_m, pct = get_tenant_overrides(dep)
        tune_cpu = is_tune_cpu_enabled(dep)
        tune_mem = is_tune_memory_enabled(dep)
        tune_lims= is_tune_limits_enabled(dep)

        patches = []
        for c in dep.spec.template.spec.containers:
            usage = filter_usage(usage_map, ns, c.name, lookback_m)
            if not usage:
                continue

            req, lim = compute_recommendations(usage, pct)
            if not req or not lim:
                continue

            container_patch = build_patch_for_container(c.name, req, lim, tune_cpu, tune_mem, tune_lims)
            if container_patch:
                patches.append(container_patch)

        if patches:
            try:
                patch_deployment(apps_api, dep, patches)
                logging.info(f"Patched {ns}/{dep.metadata.name} with new resource settings.")
            except ApiException as ex:
                logging.warning(f"Failed to patch {ns}/{dep.metadata.name}: {ex}")

    logging.info("AutoTune aggregator run completed.")

def build_usage_map(cpu_data, mem_data):
    usage_map = {}
    # CPU
    for series in cpu_data:
        metric = series.get("metric", {})
        ns = metric.get("namespace")
        ctr= metric.get("container")
        if not ns or not ctr:
            continue
        for (ts_str, val_str) in series.get("values", []):
            val = try_float(val_str)
            if val is None:
                continue
            ts = float(ts_str)
            key = (ns, ctr)
            if key not in usage_map:
                usage_map[key] = {"cpu": [], "mem": []}
            usage_map[key]["cpu"].append((ts, val))

    # Mem
    for series in mem_data:
        metric = series.get("metric", {})
        ns = metric.get("namespace")
        ctr= metric.get("container")
        if not ns or not ctr:
            continue
        for (ts_str, val_str) in series.get("values", []):
            val = try_float(val_str)
            if val is None:
                continue
            ts = float(ts_str)
            key = (ns, ctr)
            if key not in usage_map:
                usage_map[key] = {"cpu": [], "mem": []}
            usage_map[key]["mem"].append((ts, val))

    return usage_map

def try_float(s):
    try:
        return float(s)
    except ValueError:
        return None

def filter_usage(usage_map, ns, ctr, lookback_m):
    key = (ns, ctr)
    if key not in usage_map:
        return None

    now_ts = datetime.datetime.utcnow().timestamp()
    cutoff_ts = now_ts - (lookback_m*60)

    cpu_vals = [v for (ts, v) in usage_map[key]["cpu"] if ts >= cutoff_ts]
    mem_vals = [v for (ts, v) in usage_map[key]["mem"] if ts >= cutoff_ts]
    if not cpu_vals and not mem_vals:
        return None
    return {"cpu": cpu_vals, "mem": mem_vals}

def compute_recommendations(usage, pct):
    cpu_vals = usage["cpu"]
    mem_vals = usage["mem"]

    if cpu_vals:
        recommended_cpu = percentile(cpu_vals, pct)
        cpu_req_m = max(int(recommended_cpu*1000), MIN_CPU_REQUEST)
        cpu_lim_m = min(int(cpu_req_m*1.2), MAX_CPU_LIMIT)
    else:
        cpu_req_m = MIN_CPU_REQUEST
        cpu_lim_m = cpu_req_m

    if mem_vals:
        recommended_mem = percentile(mem_vals, pct)
        mem_req_mi = max(int(recommended_mem/(1024*1024)), MIN_MEM_REQUEST)
        mem_lim_mi = min(int(mem_req_mi*1.2), MAX_MEM_LIMIT)
    else:
        mem_req_mi = MIN_MEM_REQUEST
        mem_lim_mi = mem_req_mi

    requests = {"cpu_millicores": cpu_req_m, "memory_mi": mem_req_mi}
    limits   = {"cpu_millicores": cpu_lim_m, "memory_mi": mem_lim_mi}
    return (requests, limits)

def percentile(data_list, p):
    if not data_list:
        return 0
    sorted_vals = sorted(data_list)
    k = (len(sorted_vals)-1)*p/100.0
    f = int(k)
    c = f+1
    if f == c:
        return sorted_vals[f]
    d0 = sorted_vals[f]*(c-k)
    d1 = sorted_vals[c]*(k-f)
    return d0 + d1

def is_auto_tune_enabled(dep):
    lbl = (dep.metadata.labels or {}).get(AUTOTUNE_LABEL, "disabled").lower()
    return lbl == "enabled"

def get_tenant_overrides(dep):
    labels = dep.metadata.labels or {}
    lookback_m = DEFAULT_LOOKBACK_MINUTES
    pct = DEFAULT_TARGET_PERCENTILE

    if LOOKBACK_LABEL in labels:
        try:
            lookback_m = int(labels[LOOKBACK_LABEL])
        except ValueError:
            pass
    if PERCENTILE_LABEL in labels:
        try:
            pct = float(labels[PERCENTILE_LABEL])
        except ValueError:
            pass
    return (lookback_m, pct)

def is_tune_cpu_enabled(dep):
    lbl = (dep.metadata.labels or {}).get(TUNE_CPU_LABEL, "enabled").lower()
    return lbl == "enabled"

def is_tune_memory_enabled(dep):
    lbl = (dep.metadata.labels or {}).get(TUNE_MEM_LABEL, "enabled").lower()
    return lbl == "enabled"

def is_tune_limits_enabled(dep):
    lbl = (dep.metadata.labels or {}).get(TUNE_LIMITS_LABEL, "enabled").lower()
    return lbl == "enabled"

def build_patch_for_container(name, req, lim, tune_cpu, tune_mem, tune_lims):
    patch_resources = {"requests": {}, "limits": {}}
    if tune_cpu:
        patch_resources["requests"]["cpu"] = f"{req['cpu_millicores']}m"
        if tune_lims:
            patch_resources["limits"]["cpu"] = f"{lim['cpu_millicores']}m"

    if tune_mem:
        patch_resources["requests"]["memory"] = f"{req['memory_mi']}Mi"
        if tune_lims:
            patch_resources["limits"]["memory"] = f"{lim['memory_mi']}Mi"

    if not patch_resources["requests"] and not patch_resources["limits"]:
        return None
    return {"name": name, "resources": patch_resources}

def patch_deployment(apps_api, dep, container_patches):
    if not container_patches:
        return
    patch_body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": container_patches
                }
            }
        }
    }
    apps_api.patch_namespaced_deployment(
        name=dep.metadata.name,
        namespace=dep.metadata.namespace,
        body=patch_body
    )

if __name__ == "__main__":
    main()
