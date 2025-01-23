#!/usr/bin/env python3

import os
import datetime
import logging
import json

# PrometheusConnect: used to query Prometheus metrics
# kubernetes: used to patch Deployment resources and retrieve metadata
from prometheus_api_client import PrometheusConnect
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

# Configure basic logging
logging.basicConfig(level=logging.INFO)

# ------------------------------------------------------------------------------
# 1. ENVIRONMENT VARIABLES (with explicit names and clarifying comments)
# ------------------------------------------------------------------------------
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus-server.monitoring.svc.cluster.local")
"""
PROMETHEUS_URL: The base URL for querying Prometheus data. 
Default is "http://prometheus-server.monitoring.svc.cluster.local", 
but can be overridden by environment variable.
"""

LOOKBACK_MINUTES_FOR_DATA = int(os.getenv("LOOKBACK_MINUTES", "10080"))
"""
LOOKBACK_MINUTES_FOR_DATA: How many minutes of historical data to query from Prometheus.
Default is 10080 (7 days).
"""

PROMETHEUS_QUERY_STEP = os.getenv("PROMETHEUS_STEP", "30m")
"""
PROMETHEUS_QUERY_STEP: The resolution step for custom_query_range in Prometheus. 
Default "30m" means data points are fetched every 30 minutes in the time range.
"""


# ------------------------------------------------------------------------------
# 2. LABELS & ANNOTATIONS
# ------------------------------------------------------------------------------
LABEL_AUTOTUNE_ENABLED = "container-efficiency.io/autotune"
"""
LABEL_AUTOTUNE_ENABLED: Must be set to "enabled" for this Deployment to be tuned.
"""

LABEL_AUTOTUNE_FREQUENCY = "container-efficiency.io/autotune-frequency"
"""
LABEL_AUTOTUNE_FREQUENCY: Contains how often the tenant wants to be re-tuned: 
e.g. "hourly", "daily", "weekly", "biweekly".
"""

ANNOTATION_LAST_PATCH_TIME = "container-efficiency.io/last-patch-time"
"""
ANNOTATION_LAST_PATCH_TIME: Stores the timestamp (UTC ISO format) of the last 
time this Deployment was patched by the aggregator (for cooldown logic).
"""

ANNOTATION_PREVIOUS_RESOURCES = "container-efficiency.io/previous-requests-limits"
"""
ANNOTATION_PREVIOUS_RESOURCES: Stores JSON describing old vs new CPU/mem requests/limits 
for each container, so tenants can see what changed.
"""


# ------------------------------------------------------------------------------
# 3. FREQUENCY & COOLDOWN
# ------------------------------------------------------------------------------
DEFAULT_FREQUENCY_KEYWORD = "daily"
"""
DEFAULT_FREQUENCY_KEYWORD: If the tenant does not specify any frequency label, 
we assume "daily".
"""

FREQUENCY_LABEL_TO_HOURS_MAP = {
    "hourly": 1,
    "daily": 24,
    "weekly": 168,
    "biweekly": 336
}
"""
FREQUENCY_LABEL_TO_HOURS_MAP: Maps each frequency string 
to the number of hours that must pass before next patch.
Example: "daily" => 24 hours, "weekly" => 168 hours, etc.
"""


# ------------------------------------------------------------------------------
# 4. RESOURCE LIMITS & SAFETY BOUNDS
# ------------------------------------------------------------------------------
MINIMUM_CPU_REQUEST_MILLICORES = 50     # e.g. 50m
MAXIMUM_CPU_LIMIT_MILLICORES  = 4000   # e.g. 4000m => 4 CPU cores
MINIMUM_MEMORY_REQUEST_MIB     = 128   # e.g. 128Mi
MAXIMUM_MEMORY_LIMIT_MIB       = 8192  # e.g. 8192Mi => 8Gi


# ------------------------------------------------------------------------------
# MAIN ENTRY POINT
# ------------------------------------------------------------------------------
def main():
    """
    Main function: 
    1) Connect to Prometheus and Kubernetes.
    2) Query usage data for CPU & Memory (over LOOKBACK_MINUTES_FOR_DATA).
    3) List all Deployments, filter by 'autotune=enabled'.
    4) For each, check the 'autotune-frequency' label => determines cooldown hours.
    5) If enough hours have passed since last patch, compute new requests/limits, 
       patch the Deployment, and store old requests/limits in an annotation.
    """
    logging.info("Starting AutoTune aggregator with label-based frequency + storing old resources in annotation...")

    # 1) Connect to Prometheus
    prometheus_conn = PrometheusConnect(url=PROMETHEUS_URL, disable_ssl=True)

    # 2) Connect to Kubernetes
    try:
        config.load_incluster_config()
    except:
        config.load_kube_config()
    apps_v1_api = client.AppsV1Api()

    # 3) Determine the time range for Prometheus queries
    end_time_utc = datetime.datetime.utcnow()
    start_time_utc = end_time_utc - datetime.timedelta(minutes=LOOKBACK_MINUTES_FOR_DATA)
    logging.info(f"Querying Prometheus from {start_time_utc} to {end_time_utc}, step={PROMETHEUS_QUERY_STEP}")

    # 4) Example queries for CPU & Memory usage (we'll do percentile logic in compute_recommendations)
    cpu_query_string = 'max_over_time(container_cpu_usage_seconds_total{pod!=""}[5m])'
    mem_query_string = 'max_over_time(container_memory_working_set_bytes{pod!=""}[5m])'

    # Attempt to query Prometheus
    try:
        cpu_query_data = prometheus_conn.custom_query_range(
            query=cpu_query_string,
            start_time=start_time_utc,
            end_time=end_time_utc,
            step=PROMETHEUS_QUERY_STEP
        )
        mem_query_data = prometheus_conn.custom_query_range(
            query=mem_query_string,
            start_time=start_time_utc,
            end_time=end_time_utc,
            step=PROMETHEUS_QUERY_STEP
        )
    except Exception as ex_prom:
        logging.error(f"Failed to query Prometheus: {ex_prom}")
        return

    # 5) Build usage map from CPU & Memory data
    usage_map = build_usage_map(cpu_query_data, mem_query_data)

    # 6) List all Deployments across all namespaces
    try:
        all_deployments = apps_v1_api.list_deployment_for_all_namespaces().items
    except ApiException as ex_apps:
        logging.error(f"Failed to list deployments: {ex_apps}")
        return

    patch_count = 0

    for deployment_obj in all_deployments:
        # Check if 'autotune=enabled'
        if not is_autotune_enabled(deployment_obj):
            continue

        namespace_name = deployment_obj.metadata.namespace
        deployment_name = deployment_obj.metadata.name

        # Determine frequency (hourly, daily, weekly, etc.) => number of hours
        frequency_hours = get_tenant_frequency_hours(deployment_obj)

        # Check if enough time has passed since last patch
        if not is_cooldown_expired(deployment_obj, frequency_hours):
            # Not yet time to patch => skip
            logging.debug(f"Skipping {deployment_name} in {namespace_name}, cooldown not expired.")
            continue

        # Build container patches and record old/new info for annotation
        container_patch_list = []
        old_new_resources_array = []

        for container_obj in deployment_obj.spec.template.spec.containers:
            container_name = container_obj.name

            # Parse existing CPU/memory requests/limits
            (old_req_cpu_m, old_req_mem_mi,
             old_lim_cpu_m, old_lim_mem_mi) = parse_current_resources(container_obj)

            # Compute new recommended requests/limits
            recommended_requests, recommended_limits = compute_recommendations(usage_map, namespace_name, container_name)
            if not recommended_requests or not recommended_limits:
                # Possibly no usage data found => skip
                continue

            # Create patch structure for this container
            container_patch = {
                "name": container_name,
                "resources": {
                    "requests": {
                        "cpu": f"{recommended_requests['cpu']}m",
                        "memory": f"{recommended_requests['mem']}Mi"
                    },
                    "limits": {
                        "cpu": f"{recommended_limits['cpu']}m",
                        "memory": f"{recommended_limits['mem']}Mi"
                    }
                }
            }
            container_patch_list.append(container_patch)

            # Build a record of old vs. new
            old_new_entry = {
                "container": container_name,
                "oldRequests": {
                    "cpu": f"{old_req_cpu_m}m",
                    "memory": f"{old_req_mem_mi}Mi"
                },
                "oldLimits": {
                    "cpu": f"{old_lim_cpu_m}m",
                    "memory": f"{old_lim_mem_mi}Mi"
                },
                "newRequests": {
                    "cpu": f"{recommended_requests['cpu']}m",
                    "memory": f"{recommended_requests['mem']}Mi"
                },
                "newLimits": {
                    "cpu": f"{recommended_limits['cpu']}m",
                    "memory": f"{recommended_limits['mem']}Mi"
                }
            }
            old_new_resources_array.append(old_new_entry)

        # If we have patches to apply
        if container_patch_list:
            # We'll store last patch time + old/new resources in the same patch
            patch_body = {
                "metadata": {
                    "annotations": {
                        # Update the last patch time annotation
                        ANNOTATION_LAST_PATCH_TIME: datetime.datetime.utcnow().isoformat(),
                        # Also store old/new requests/limits
                        ANNOTATION_PREVIOUS_RESOURCES: json.dumps(old_new_resources_array)
                    }
                },
                "spec": {
                    "template": {
                        "spec": {
                            "containers": container_patch_list
                        }
                    }
                }
            }

            # Perform the patch
            try:
                apps_v1_api.patch_namespaced_deployment(deployment_name, namespace_name, patch_body)
                logging.info(f"Patched {deployment_name} in {namespace_name}, stored old->new resources annotation.")
                patch_count += 1
            except ApiException as ex_patch:
                logging.warning(f"Failed patching {deployment_name} in {namespace_name}: {ex_patch}")

    logging.info(f"AutoTune aggregator finished. Patched {patch_count} Deployments.")


# ------------------------------------------------------------------------------
# HELPER FUNCTIONS
# ------------------------------------------------------------------------------

def build_usage_map(cpu_data, mem_data):
    """
    Build a dictionary: usage_map[(namespace, container)] = { "cpu":[], "mem":[] }.
    We'll store max data points for CPU and memory. 
    Later, we do percentile logic in compute_recommendations.
    """
    usage_map = {}

    # CPU
    for cpu_series in cpu_data:
        metric_labels = cpu_series.get("metric", {})
        ns = metric_labels.get("namespace")
        ctr = metric_labels.get("container")
        if not ns or not ctr:
            continue

        values = cpu_series.get("values", [])
        data_points = [try_parse_float(v[1]) for v in values if try_parse_float(v[1]) is not None]
        if data_points:
            key = (ns, ctr)
            if key not in usage_map:
                usage_map[key] = {"cpu": [], "mem": []}
            usage_map[key]["cpu"].append(max(data_points))

    # Memory
    for mem_series in mem_data:
        metric_labels = mem_series.get("metric", {})
        ns = metric_labels.get("namespace")
        ctr = metric_labels.get("container")
        if not ns or not ctr:
            continue

        values = mem_series.get("values", [])
        data_points = [try_parse_float(v[1]) for v in values if try_parse_float(v[1]) is not None]
        if data_points:
            key = (ns, ctr)
            if key not in usage_map:
                usage_map[key] = {"cpu": [], "mem": []}
            usage_map[key]["mem"].append(max(data_points))

    return usage_map


def compute_recommendations(usage_map, namespace_name, container_name):
    """
    Calculate recommended resource requests & limits using a 95th percentile approach.
    Return two dicts: 
    - recommended_requests = {"cpu": <millicores>, "mem": <Mi>}
    - recommended_limits   = {"cpu": <millicores>, "mem": <Mi>}
    """
    key = (namespace_name, container_name)
    if key not in usage_map:
        return None, None

    cpu_samples = usage_map[key]["cpu"]
    mem_samples = usage_map[key]["mem"]

    if not cpu_samples and not mem_samples:
        return None, None

    # CPU in millicores
    if cpu_samples:
        cpu_95th_cores = percentile(cpu_samples, 95)
        # Convert float cores -> millicores
        recommended_cpu_m = int(max(cpu_95th_cores * 1000, MINIMUM_CPU_REQUEST_MILLICORES))
        recommended_cpu_m = min(recommended_cpu_m, MAXIMUM_CPU_LIMIT_MILLICORES)
    else:
        recommended_cpu_m = MINIMUM_CPU_REQUEST_MILLICORES

    # Memory in Mi
    if mem_samples:
        mem_95th_bytes = percentile(mem_samples, 95)
        recommended_mem_mi = int(max(mem_95th_bytes / (1024*1024), MINIMUM_MEMORY_REQUEST_MIB))
        recommended_mem_mi = min(recommended_mem_mi, MAXIMUM_MEMORY_LIMIT_MIB)
    else:
        recommended_mem_mi = MINIMUM_MEMORY_REQUEST_MIB

    # We'll add a 20% overhead for the limit
    cpu_limit_m = min(int(recommended_cpu_m * 1.2), MAXIMUM_CPU_LIMIT_MILLICORES)
    mem_limit_mi = min(int(recommended_mem_mi * 1.2), MAXIMUM_MEMORY_LIMIT_MIB)

    req_dict = {
        "cpu": recommended_cpu_m,
        "mem": recommended_mem_mi
    }
    lim_dict = {
        "cpu": cpu_limit_m,
        "mem": mem_limit_mi
    }
    return req_dict, lim_dict


def parse_current_resources(container_obj):
    """
    Parse existing container resources (requests & limits).
    Return a 4-tuple of integers:
      (old_req_cpu_millicores, old_req_mem_mi, old_lim_cpu_millicores, old_lim_mem_mi).
    """
    requests_info = container_obj.resources.requests or {}
    limits_info   = container_obj.resources.limits or {}

    old_req_cpu_str   = requests_info.get("cpu", "50m")
    old_req_mem_str   = requests_info.get("memory", "128Mi")
    old_lim_cpu_str   = limits_info.get("cpu", "1000m")
    old_lim_mem_str   = limits_info.get("memory", "512Mi")

    old_req_cpu_m   = parse_cpu_millicores(old_req_cpu_str)
    old_req_mem_mi  = parse_memory_in_mi(old_req_mem_str)
    old_lim_cpu_m   = parse_cpu_millicores(old_lim_cpu_str)
    old_lim_mem_mi  = parse_memory_in_mi(old_lim_mem_str)

    return (old_req_cpu_m, old_req_mem_mi, old_lim_cpu_m, old_lim_mem_mi)


def parse_cpu_millicores(cpu_string):
    """
    Convert CPU string (e.g. "100m", "1") into integer millicores.
      - "100m" => 100
      - "1"    => 1000
      - "2.5"  => 2500, etc.
    """
    if cpu_string.endswith("m"):
        return int(cpu_string[:-1])
    else:
        float_val = float(cpu_string)
        return int(float_val * 1000)


def parse_memory_in_mi(memory_string):
    """
    Convert memory string (e.g. "128Mi", "1Gi") into integer Mi.
      - "128Mi" => 128
      - "1Gi"   => 1024
      - fallback => parse as float MB => convert to int
    """
    if memory_string.endswith("Gi"):
        float_val = float(memory_string[:-2])
        return int(float_val * 1024)
    elif memory_string.endswith("Mi"):
        float_val = float(memory_string[:-2])
        return int(float_val)
    else:
        # fallback parse as MB
        float_val = float(memory_string)
        return int(float_val)


def percentile(values_list, percentile_num):
    """
    Return the 'percentile_num'-th percentile of 'values_list'.
    E.g. percentile(values, 95) => 95th percentile.
    Implementation uses sorted list + linear interpolation.
    """
    if not values_list:
        return 0.0
    sorted_vals = sorted(values_list)
    p = max(0, min(100, percentile_num))

    k_index = (len(sorted_vals) - 1) * p / 100.0
    lower_index = int(k_index)
    upper_index = lower_index + 1

    if upper_index >= len(sorted_vals):
        # p=100 or so
        return sorted_vals[-1]
    if lower_index == upper_index:
        return sorted_vals[lower_index]

    d0 = sorted_vals[lower_index] * (upper_index - k_index)
    d1 = sorted_vals[upper_index]  * (k_index - lower_index)
    return d0 + d1


def try_parse_float(value_str):
    """
    Safely parse a string as float, return None if fails.
    """
    try:
        return float(value_str)
    except ValueError:
        return None


def is_autotune_enabled(deployment_obj):
    """
    Check if deployment_obj.metadata.labels[LABEL_AUTOTUNE_ENABLED] == "enabled".
    If not, we skip.
    """
    label_dict = deployment_obj.metadata.labels or {}
    return (label_dict.get(LABEL_AUTOTUNE_ENABLED, "disabled").lower() == "enabled")


def get_tenant_frequency_hours(deployment_obj):
    """
    Read the 'container-efficiency.io/autotune-frequency' label. 
    If missing => default "daily" => 24 hours.
    If present => convert "hourly" => 1, "weekly" => 168, etc.
    """
    label_dict = deployment_obj.metadata.labels or {}
    freq_label_value = label_dict.get(LABEL_AUTOTUNE_FREQUENCY, DEFAULT_FREQUENCY_KEYWORD).lower()
    return FREQUENCY_LABEL_TO_HOURS_MAP.get(freq_label_value, FREQUENCY_LABEL_TO_HOURS_MAP[DEFAULT_FREQUENCY_KEYWORD])


def is_cooldown_expired(deployment_obj, freq_hours):
    """
    Check if enough hours have passed since last patch time.
    last patch time is stored in 'container-efficiency.io/last-patch-time'.
    If not present => return True (never patched).
    If present => compare (now - last_time) >= freq_hours.
    """
    ann_dict = deployment_obj.metadata.annotations or {}
    last_patch_str = ann_dict.get(ANNOTATION_LAST_PATCH_TIME)
    if not last_patch_str:
        # never patched => we can patch now
        return True

    try:
        last_patch_dt = datetime.datetime.fromisoformat(last_patch_str)
    except ValueError:
        logging.warning(f"Invalid last-patch-time on {deployment_obj.metadata.name}, ignoring => patch.")
        return True

    now_utc = datetime.datetime.utcnow()
    elapsed_hours = (now_utc - last_patch_dt).total_seconds() / 3600.0
    return (elapsed_hours >= freq_hours)


# If run as a script:
if __name__ == "__main__":
    main()
