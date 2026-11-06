"""PromQL generation for master/worker separation."""

from metrics.prometheus import PrometheusClient


def test_dotted_control_plane_name_is_escaped_for_promql_strings():
    queries = []

    def query(promql):
        queries.append(promql)
        if "kube_node_role" in promql and "count" not in promql:
            return {
                "resultType": "vector",
                "result": [{"metric": {"node": "ip-10-0-1-5.ec2.internal"}}],
            }
        return {"resultType": "vector", "result": [{"value": [0, "50"]}]}

    client = PrometheusClient(prometheus_url="http://prom")
    client.query = query
    client.get_cluster_metrics()

    filtered = [q for q in queries if "instance" in q and "~" in q]
    assert filtered, "expected instance-filtered worker/master queries"
    for q in filtered:
        # Inside a double-quoted PromQL string a regex-literal dot must be
        # written as \\. -- a bare \. is rejected as an unknown escape.
        assert r"ip-10-0-1-5\\.ec2\\.internal" in q
        assert r"5\.ec2" not in q.replace(r"\\.", "")
