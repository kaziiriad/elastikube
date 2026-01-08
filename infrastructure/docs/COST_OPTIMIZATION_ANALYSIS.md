# K3s Autoscaler Infrastructure - Cost Optimization Analysis

## Current Monthly Cost Breakdown (ap-southeast-1 Region)

| Component | Quantity | Unit Price | Monthly Cost | Necessity |
|-----------|----------|------------|--------------|-----------|
| **EC2 Instances** |
| Bastion Host (t3.micro) | 1 | $0.0068/hr | **$4.91** | 🔴 OPTIONAL |
| Master Node (t3.small) | 1 | $0.0136/hr | **$9.79** | ✅ ESSENTIAL |
| Worker Nodes (t3.small × 2) | 2 | $0.0136/hr | **$19.58** | ✅ ESSENTIAL |
| **Networking** |
| NAT Gateway | 1 | $0.045/hr | **$32.40** | 🟠 REDUCIBLE |
| Elastic IP | 1 | $0.005/hr | **$3.60** | 🟠 REDUCIBLE |
| Internet Gateway | 1 | $0 | **$0** | ✅ ESSENTIAL |
| **DynamoDB** |
| Cluster State Table | 1 | On-demand | ~$1-5 | ✅ ESSENTIAL |
| WAL Table | 1 | On-demand | ~$1-5 | ✅ ESSENTIAL |
| **Lambda** |
| Autoscaler Function | 1 | $0.00001667/GB-s | ~$0.50 | ✅ ESSENTIAL |
| Lambda Log Group (7 days) | 1 | $0.50/GB | ~$0.20 | ✅ ESSENTIAL |
| **SSM & Secrets Manager** |
| SSM Parameter | 1 | $0.05/10,000 | ~$0.01 | ✅ ESSENTIAL |
| Secrets Manager Secret | 1 | $0.40/month | **$0.40** | ✅ ESSENTIAL |
| **EventBridge** |
| Scheduled Rule (2 min) | 1 | $1.00/million | ~$0.22 | ✅ ESSENTIAL |
| **CloudWatch Alarms** |
| Metric Alarms (4 alarms) | 4 | Free tier | **$0** | 🔴 OPTIONAL |

### **Current Total: ~$73-81/month**

---

## Cost Optimization Opportunities

### 🟡 **HIGH IMPACT: Networking ($36/month savings)**

#### 1. Remove Bastion Host ($4.91/month savings)
```python
# Line 519-529: REMOVE THIS
bastion_instance = ec2.Instance(
    'bastion-instance',
    instance_type="t3.micro",  # $4.91/month
    ...
)
```

**Why Optional**: Bastion is only for SSH access. Alternatives:
- Use AWS Systems Manager Session Manager (free with SSM agent installed)
- Use EC2 Instance Connect (one-time $0.003 per connection)
- Direct access if within VPN

**Impact**: Remove bastion_security_group and bastion_instance

---

#### 2. Eliminate NAT Gateway ($32.40/month savings!)
```python
# Line 123-130: REMOVE THIS - BIGGEST SAVINGS!
nat_gateway = ec2.NatGateway(
    'nat-gateway',
    subnet_id=public_subnet.id,
    allocation_id=eip.id,  # Also saves $3.60/month EIP
    ...
)
```

**Why Optional**: NAT Gateway is only needed if instances in private subnet need outbound internet access.

**Alternatives**:
- Use **public subnets** for K3s nodes (simpler, cheaper)
- Use NAT Instance ($4.91/month with t3.micro) instead of NAT Gateway
- Remove outbound internet entirely (K3s only needs internal VPC communication)

**Impact**: Remove nat_gateway, eip, private_route_table, private_route_table_association

---

### 🟠 **MEDIUM IMPACT: DynamoDB Optimization ($5-15/month savings)**

#### 3. Remove Global Secondary Indexes
```python
# Line 264-272: OPTIONAL - Remove index
global_secondary_indexes=[
    dynamodb.TableGlobalSecondaryIndexArgs(
        name="ScalingStatusIndex",  # ← NOT USED IN CODE
        ...
    )
]

# Line 290-297: OPTIONAL - Remove index
global_secondary_indexes=[
    dynamodb.TableGlobalSecondaryIndexArgs(
        name="IncompleteOperations",  # ← NOT USED IN CODE
        ...
    )
]
```

**Why Optional**: These indexes are defined but **never queried** in the Lambda code.

**Savings**: ~$2-8/month per GSI (depends on data size)

**Impact**: Reduce DynamoDB costs by 50-70%

---

#### 4. Disable Point-in-Time Recovery
```python
# Line 260-262: CHANGE FROM
point_in_time_recovery=dynamodb.TablePointInTimeRecoveryArgs(
    enabled=True,  # ← Costs $0.25/GB/month
)

# TO:
point_in_time_recovery=None  # Save ~$0.50-2/month
```

**Why Optional**: PITR is useful for production but overkill for development/testing.

---

### 🔴 **LOW IMPACT: CloudWatch Alarms ($0 savings, but reduces complexity)**

#### 5. Remove Unused CloudWatch Alarms
```python
# Line 647-701: ALL ALARMS ARE UNUSED!
# These alarms have no SNS topics or actions attached
high_cpu_alarm = aws.cloudwatch.MetricAlarm(...)      # ← NO ACTION
scaling_failure_alarm = aws.cloudwatch.MetricAlarm(...)  # ← NO ACTION
provisioning_timeout_alarm = aws.cloudwatch.MetricAlarm(...)  # ← NO ACTION
lock_timeout_alarm = aws.cloudwatch.MetricAlarm(...)  # ← NO ACTION
```

**Why Optional**: Alarms without notifications are just visual in CloudWatch console.

**Savings**: $0 (alarms are free), but reduces code complexity

**Recommendation**: Keep 1-2 critical alarms, add SNS notifications if needed

---

### 🟢 **MINOR IMPACT: Lambda Tuning ($0.10-0.30/month savings)**

#### 6. Reduce Lambda Memory
```python
# Line 601: CHANGE FROM
memory_size=256,  # 256 MB = $0.0000002083/GB-s

# TO:
memory_size=128,  # 128 MB = 50% cheaper
```

**Why Safe**: Lambda doesn't use much memory (mostly waiting for I/O)

**Savings**: ~50% of Lambda compute costs (~$0.25/month)

---

#### 7. Reduce Lambda Timeout
```python
# Line 600: CHANGE FROM
timeout=300,  # 5 minutes

# TO:
timeout=60,   # 1 minute is sufficient for scaling decisions
```

**Why Safe**: Scaling decision takes <10 seconds; 1 minute is plenty

---

#### 8. Reduce Log Retention
```python
# Line 586: CHANGE FROM
retention_in_days=7,  # 7 days

# TO:
retention_in_days=1,  # 1 day for dev, 3 days for prod
```

**Savings**: Minimal ($0.10-0.20/month), but reduces storage

---

### 🔵 **ZERO COST: Configuration Changes**

#### 9. Use Spot Instances for Workers
```python
# Line 544, 556: ADD
worker_instance_1 = ec2.Instance(
    'worker-instance-1',
    instance_type=worker_instance_type,
    instance_lifecycle="spot",  # ← ADD THIS - 70-90% discount!
    spot_options=ec2.InstanceSpotOptionsArgs(
        spot_instance_type="persistent",  # Auto-restart if interrupted
    ),
    ...
)
```

**Savings**: **70-90% off worker instances** ($13.71-17.62/month savings)

**Risk**: Spot instances can be interrupted (but K3s workers are stateless, so safe!)

---

#### 10. Remove Permanent Workers (Scale to Zero)
```python
# Line 543-565: KEEP ONLY 1 WORKER, OR 0 WITH AUTOSCALING
# The autoscaler will add workers as needed
worker_instance_1 = ec2.Instance(...)  # Keep 1 as minimum
# REMOVE worker_instance_2 - autoscaler will create it when needed
```

**Savings**: $9.79/month per removed worker (when idle)

---

## 🎯 **Optimized Infrastructure Recommendations**

### **Tier 1: Essential Changes (Must Do)**
| Change | Monthly Savings | Effort |
|--------|-----------------|--------|
| Remove NAT Gateway, use public subnets | **$32.40** | Medium |
| Remove Bastion Host, use Session Manager | **$4.91** | Low |
| Remove unused Global Secondary Indexes | **$4-8** | Low |
| **Total Tier 1** | **$41-45/month** | |

### **Tier 2: High Impact (Should Do)**
| Change | Monthly Savings | Effort |
|--------|-----------------|--------|
| Use Spot Instances for workers | **$13-17** | Low |
| Remove 1 permanent worker (scale from 1) | **$9.79** | Low |
| Disable PITR on DynamoDB | **$0.50-2** | Low |
| **Total Tier 2** | **$23-29/month** | |

### **Tier 3: Fine Tuning (Nice to Have)**
| Change | Monthly Savings | Effort |
|--------|-----------------|--------|
| Reduce Lambda memory to 128MB | **$0.25** | Trivial |
| Reduce log retention to 1-3 days | **$0.15** | Trivial |
| Remove unused CloudWatch Alarms | **$0** | Low |
| **Total Tier 3** | **$0.40** | |

---

## 📊 **Cost Comparison**

| Configuration | Monthly Cost | Annual Cost |
|---------------|--------------|-------------|
| **Current (All Features)** | **$73-81** | **$876-972** |
| Tier 1 Optimized | **$32-36** | **$384-432** |
| Tier 1 + Tier 2 Optimized | **$8-14** | **$96-168** |
| Tier 1 + Tier 2 + Tier 3 | **$7-13** | **$84-156** |

### **Total Potential Savings: 80-90% cost reduction!**

---

## 🔧 **Implementation Priority**

### **Quick Wins (1-2 hours)**
1. Comment out NAT Gateway + EIP → $36 savings
2. Comment out Bastion Host + SG → $5 savings
3. Remove GSI from DynamoDB tables → $6 savings
4. Reduce Lambda memory to 128MB → $0.25 savings

### **Medium Effort (2-4 hours)**
5. Switch to public subnets for K3s nodes
6. Implement Spot Instances for workers
7. Remove 1 permanent worker
8. Disable PITR on DynamoDB

### **Code Changes Required**
- Update security groups for public subnet deployment
- Update Lambda subnet_id environment variable
- Add Spot Instance configuration
- Test Session Manager access for SSH

---

## ⚠️ **Trade-offs to Consider**

| Optimization | Benefit | Trade-off |
|--------------|---------|-----------|
| Remove NAT Gateway | $32/month savings | Instances need public IPs (less secure) |
| Remove Bastion | $5/month savings | Use Session Manager or VPN for SSH |
| Spot Instances | 70-90% savings | Workers can be interrupted (~5% chance) |
| Remove GSI | $6/month savings | Manual DynamoDB scans if queries needed |
| Public Subnets | No NAT cost | Direct internet access (security consideration) |

---

## 🎯 **Recommended Target Architecture**

```
Cost-Optimized Stack ($8-14/month):
┌─────────────────────────────────────────────────────┐
│ EC2 Instances (Public Subnet)                        │
│  ├── Master: 1 × t3.small ($9.79)                   │
│  ├── Worker: 1 × t3.small Spot ($1.46-2.93)         │
│  └── Autoscaler adds 0-9 more workers as needed     │
├─────────────────────────────────────────────────────┤
│ Lambda: 128MB, 1min timeout ($0.25)                  │
├─────────────────────────────────────────────────────┤
│ DynamoDB: On-demand, no GSI, no PITR ($2-4)         │
├─────────────────────────────────────────────────────┤
│ SSM + Secrets Manager ($0.41)                        │
├─────────────────────────────────────────────────────┤
│ EventBridge Rule ($0.22)                             │
└─────────────────────────────────────────────────────┘

Total: ~$8-14/month (down from $73-81/month!)
```

---

## 📝 **Next Steps**

1. **Create cost-optimized branch**: `git checkout -b feature/cost-optimized`
2. **Apply Tier 1 changes** (comment out NAT + Bastion + GSI)
3. **Test deployment** with `pulumi up --stack cost-optimized`
4. **Verify autoscaler functionality** still works
5. **Apply Tier 2 changes** (Spot instances, remove worker)
6. **Run load tests** to ensure scaling works
7. **Calculate actual savings** from AWS Cost Explorer