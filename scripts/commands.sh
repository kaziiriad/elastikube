AWS_PROFILE=k3s-temp-user pulumi up -y

ansible-playbook site.yml -i inventory/hosts.ini --limit master

# Check if CloudWatch Agent pods are running
kubectl get pods -n monitoring -l app=cloudwatch-agent

# Check CloudWatch Agent logs for errors
kubectl logs -n monitoring -l app=cloudwatch-agent --tail=100

# Look for successful metric pushes
kubectl logs -n monitoring -l app=cloudwatch-agent | grep -i "metric\|error"

kubectl port-forward -n monitoring svc/prometheus-nodeport 30900:9090 &
curl http://localhost:30900/api/v1/query?query=up

# 2. Verify node-exporter is scraping
kubectl get pods -n kube-system -l app.kubernetes.io/name=node-exporter
curl http://localhost:30900/api/v1/query?query=node_cpu_seconds_total

# 3. Check CloudWatch Agent status
kubectl describe pod -n monitoring -l app=cloudwatch-agent
kubectl logs -n monitoring -l app=cloudwatch-agent --tail=50

# 4. Verify IAM permissions on master node
aws sts get-caller-identity  # Run from master node via SSH

# 5. List CloudWatch metrics
aws cloudwatch list-metrics --namespace ContainerInsights/Prometheus --region ap-southeast-1

AWS_PROFILE=k3s-temp-user ssh k3s-master kubectl get pods

kubectl delete deployment cpu-stress