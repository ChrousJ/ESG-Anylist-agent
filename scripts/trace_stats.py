import os
import re
import statistics
from collections import defaultdict

def get_trace_stats(log_file="api.log"):
    """
    Parse the api.log file and compute P50, P95, and average latency 
    per node based on 'done <ms>' messages.
    """
    if not os.path.exists(log_file):
        return {"error": "Log file not found"}
        
    # Matches: [esg-20260317-e44dc0] [context] done 1ms
    pattern = re.compile(r"\[esg-[0-9a-fA-F-]+\]\s+\[([a-zA-Z0-9_]+)\]\s+done\s+(\d+)ms")
    
    node_durations = defaultdict(list)
    
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                match = pattern.search(line)
                if match:
                    node_name = match.group(1)
                    duration = int(match.group(2))
                    node_durations[node_name].append(duration)
    except Exception as e:
        return {"error": str(e)}
        
    stats = {}
    for node, durations in node_durations.items():
        if not durations:
            continue
        durations.sort()
        count = len(durations)
        avg = sum(durations) / count
        p50 = durations[int(count * 0.50)]
        p95 = durations[int(count * 0.95)]
        
        stats[node] = {
            "count": count,
            "avg_ms": round(avg, 2),
            "p50_ms": p50,
            "p95_ms": p95
        }
    
    total_traces = max((s["count"] for s in stats.values()), default=0)
    
    return {
        "node_stats": stats, 
        "total_traces_approx": total_traces
    }

if __name__ == "__main__":
    import json
    print(json.dumps(get_trace_stats(), indent=2))
