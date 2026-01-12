# Bootstrap Test Lambda

A standalone Lambda function for testing the K3s worker bootstrap script end-to-end.

## Purpose

This Lambda function launches a test EC2 instance with the bootstrap script from S3 and monitors its execution to verify:
- SSM Agent installation
- Master IP retrieval from SSM Parameter Store
- K3s join token retrieval from Secrets Manager
- K3s agent installation and cluster join

## Usage

### Build the Lambda

```bash
cd /mnt/e/custom_autoscaler/production/bootstrap-test-lambda
./build.sh
```

### Deploy with Pulumi

Update the Pulumi infrastructure to include this Lambda function (see `infrastructure/pulumi/__main__.py`).

### Invoke the Lambda

**Launch a test instance:**
```bash
aws lambda invoke --function-name bootstrap-test-lambda \
  --payload '{"action": "launch"}' \
  --cli-binary-format raw-in-base64-out \
  response.json && cat response.json
```

**Describe instance status:**
```bash
aws lambda invoke --function-name bootstrap-test-lambda \
  --payload '{"action": "describe", "instance_id": "i-xxxxxxxxx"}' \
  --cli-binary-format raw-in-base64-out \
  response.json && cat response.json
```

**Terminate test instance:**
```bash
aws lambda invoke --function-name bootstrap-test-lambda \
  --payload '{"action": "terminate", "instance_id": "i-xxxxxxxxx"}' \
  --cli-binary-format raw-in-base64-out \
  response.json && cat response.json
```

## Environment Variables

The Lambda requires the following environment variables (set by Pulumi):

- `SUBNET_ID`: Subnet ID to launch instance in
- `SECURITY_GROUP_ID`: Security group ID
- `IAM_INSTANCE_PROFILE`: IAM instance profile name
- `AMI_ID`: AMI ID for the instance
- `INSTANCE_TYPE`: EC2 instance type (default: t3.small)
- `USER_DATA_S3_BUCKET`: S3 bucket containing bootstrap script
- `USER_DATA_S3_KEY`: S3 key for bootstrap script (e.g., `user-data/worker-bootstrap.sh`)

## Response Format

### Launch Response
```json
{
  "statusCode": 200,
  "body": {
    "message": "Test instance launched successfully",
    "instance_id": "i-xxxxxxxxx",
    "test_id": "abc12345",
    "config": {
      "ami": "ami-xxxxxxxx",
      "instance_type": "t3.small",
      "subnet": "subnet-xxxxxxxx"
    },
    "tags": {...},
    "next_actions": {
      "describe": "Invoke with action=describe,instance_id=i-xxxxxxxxx",
      "terminate": "Invoke with action=terminate,instance_id=i-xxxxxxxxx"
    }
  }
}
```

### Describe Response
```json
{
  "statusCode": 200,
  "body": {
    "instance_id": "i-xxxxxxxxx",
    "state": {"Name": "running", "Code": 16},
    "private_ip": "10.0.2.xxx",
    "public_ip": null,
    "launch_time": "2025-01-12T10:30:00Z",
    "console_output_available": true,
    "console_output": "...",
    "bootstrap_progress": {
      "steps_completed": ["[1/7] Installing dependencies...", "[2/7] Installing SSM Agent..."],
      "current_step": "[2/7] Installing SSM Agent...",
      "ssm_agent_installed": true,
      "k3s_installed": false,
      "errors": []
    }
  }
}
```

## IAM Permissions Required

The Lambda needs the following permissions:
- `ec2:RunInstances` - Launch test instances
- `ec2:TerminateInstances` - Terminate test instances
- `ec2:DescribeInstances` - Query instance status
- `ec2:DescribeInstanceStatus` - Check instance health
- `ec2:GetConsoleOutput` - Fetch console output for debugging
- `ec2:CreateTags` - Tag instances
- `s3:GetObject` - Fetch bootstrap script from S3
- `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` - CloudWatch logging

## Testing Workflow

1. **Launch**: Invoke Lambda with `action=launch` to create a test instance
2. **Wait**: Wait 2-3 minutes for instance to boot and run bootstrap script
3. **Describe**: Invoke Lambda with `action=describe` to check bootstrap progress
4. **Verify**: Check console output for SSM Agent and K3s installation
5. **Cleanup**: Invoke Lambda with `action=terminate` to clean up test instance