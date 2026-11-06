# ElastiKube — Video Presentation Coverage

**Duration:** 10 minutes max | **Format:** Screen record + voice, face cam optional

---

## 1. Problem Statement (1–1.5 min)

- K3s clusters on AWS need dynamic worker node scaling
- Manual scaling is slow, error-prone, wastes resources
- Key pain points: pending pods, CPU spikes (flash sales), multi-AZ high availability

## 2. Problem Breakdown & Requirements Analysis (1–1.5 min)

**Functional requirements:**
- Scale up when CPU ≥ threshold OR pending pods ≥ 1
- Scale down when CPU AND memory both low (LIFO strategy)
- Multi-AZ distribution with round-robin launch, LIFO termination
- Crash recovery (WAL for incomplete operations)
- Graceful node draining before removal

**Non-functional:**
- 2-minute decision interval via EventBridge
- Distributed locking (DynamoDB) to prevent concurrent scaling
- Spot instance support with automatic on-demand fallback
- Time-aware thresholds (peak 9AM–9PM vs off-peak)

## 3. Solution Statement & System Design Overview (3–4 min)

**Architecture:**
```
EventBridge (2-min) → Decision Lambda → ScaleUp/ScaleDown events
                                            ↓
                        Scale-Up / Scale-Down / Cleanup Lambdas
                                            ↓
                                    EC2 API
                                            ↓
                        DynamoDB (state, WAL, locks)

Prometheus (in-cluster) ↔ Decision Lambda (queries metrics)
CloudWatch (logs, metrics, 17 alarms)
```

**Key design decisions to explain:**
- Why Lambda + EventBridge (event-driven, cost-efficient, decoupled)
- Why DynamoDB (PAY_PER_REQUEST, TTL, conditional writes for locking)
- Why time-aware thresholds (prevent thrashing at peak hours)
- Why LIFO for scale-down (complements round-robin, natural AZ rebalancing)
- Flash sale detection (>30% CPU spike bypasses cooldowns)

**Multi-AZ architecture:**
- 3 private subnets across ap-southeast-1a/b/c
- Master + permanent workers in AZ-a
- Scaled workers round-robin across AZs
- Single NAT Gateway for cost optimization

**Crash recovery:**
- WAL tracks all operations; incomplete ops >10 min marked FAILED
- Distributed lock prevents concurrent Lambda executions

## 4. Live Demo / Walkthrough (2–3 min)

Pick 2–3 scenarios:

| Scenario | What to show |
|----------|-------------|
| **Scale up (CPU spike)** | Deploy cpu-stress workload, show Decision Lambda log, watch EC2 launch |
| **Scale up (pending pods)** | Deploy pending-pod workload, trigger scale-up |
| **Scale down (LIFO)** | Delete workloads, Lambda selects most recent non-permanent worker, drain + terminate |
| **Spot interruption** | EventBridge 2-min warning → Cleanup Lambda handles drain |

**Where to see it:**
- CloudWatch Logs: `/aws/lambda/k3s-autoscaler-function`
- DynamoDB: `k3s-cluster-state` table (worker_count, cooldowns)
- EC2 Console: instances launch/terminate with `CreatedBy=autoscaler` tags

## 5. Challenges & Solutions (1–1.5 min)

| Challenge | Solution |
|-----------|----------|
| Concurrent Lambda executions | DynamoDB conditional writes (optimistic locking, 10s timeout) |
| Nodes join slowly / fail | 5 idempotency checks before launching (bootstrap creds, cooldown, scaling_in_progress, pending instances, distributed lock) |
| Prometheus goes down | Graceful degradation — assume 100% CPU (blocks scale-down), pending pods still trigger scale-up |
| Spot instance termination | EventBridge 2-min warning → Cleanup Lambda drains node before AWS terminates |
| State drift across systems | Reconciliation every 60s syncs Redis/Docker/K8s/MongoDB |

## 6. Timeline (30 sec)

| Phase | Duration | Activities |
|-------|----------|-----------|
| Research & Design | 2 weeks | Architecture decisions, DynamoDB schema, IAM policies |
| Prototype | 3 weeks | Local K3s cluster, Python autoscaler with Redis/MongoDB |
| Lambda Migration | 2 weeks | Refactor to event-driven Lambdas, add DynamoDB state |
| Production Infrastructure | 2 weeks | Pulumi IaC, multi-AZ VPC, EC2 seed nodes |
| Testing & Refinement | 1 week | Load testing, spot interruption tests, alarm tuning |

---

## Slide Sequence Recommendation

1. **Title** — ElastiKube: Event-Driven Autoscaling for K3s on AWS
2. **Problem** — The scaling challenge
3. **Requirements** — Functional & non-functional
4. **Architecture** — Full system diagram (EventBridge → Lambdas → EC2)
5. **Key Decisions** — Why this approach (5 design decisions)
6. **Multi-AZ Design** — Round-robin + LIFO explanation
7. **State Management** — DynamoDB tables, WAL, distributed lock
8. **Time-Aware Scaling** — Thresholds table + flash sale detection
9. **Demo** — 2–3 scenarios (scale up, scale down, spot interruption)
10. **Challenges** — Table of challenges + solutions
11. **Timeline** — 4-phase timeline
12. **Thank you / Q&A**