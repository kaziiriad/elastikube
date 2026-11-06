#!/bin/bash
# Launch the master instance.
set -euo pipefail
export AWS_PROFILE=poridhi-aws AWS_DEFAULT_REGION=ap-southeast-1
aws ec2 run-instances \
    --image-id ami-0d95f2f0cc4ab4566 \
    --instance-type t3.small \
    --subnet-id subnet-092d14e0edb9d96c7 \
    --security-group-ids sg-0833c89fb447ef27a \
    --iam-instance-profile Name=k3s-worker \
    --associate-public-ip-address \
    --key-name elastikube-test \
    --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":20,"VolumeType":"gp3"}}]' \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=k3s-master},{Key=Role,Value=k3s-master},{Key=Permanent,Value=true},{Key=Cluster,Value=production-k3s},{Key=Project,Value=elastikube-test}]" \
    --query 'Instances[0].{Id:InstanceId,State:State.Name,Image:ImageId,Subnet:NetworkInterfaces[0].SubnetId}' \
    --output json
