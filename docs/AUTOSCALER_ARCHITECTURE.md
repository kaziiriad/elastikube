# K3s Autoscaler Architecture

## System Overview

```mermaid
graph TB
    subgraph "AWS Cloud"
        EB[EventBridge<br/>Schedule: 2min]
        Lambda[Lambda Function<br/>Autoscaler]

        subgraph "State & Locking"
            DDB1[(DynamoDB<br/>Cluster State)]
            DDB2[(DynamoDB<br/>WAL)]
            S3[(S3 Bucket<br/>K3s Token)]
        end

        subgraph "Compute Layer"
            Master[EC2 Master<br/>k3s-master]
            W1[EC2 Worker 1<br/>k3s-worker-1*]
            W2[EC2 Worker 2<br/>k3s-worker-2*]
            Wdyn[Dynamic Workers<br/>k3s-worker-N]
        end

        CW[CloudWatch<br/>Metrics]
        ALRM[CloudWatch<br/>Alarms]
    end

    subgraph "K3s Cluster"
        K8s[Kubernetes API<br/>:6443]
        NS[monitoring namespace]

        subgraph "Pods"
            PROM_POD[Prometheus Pod<br/>Service: NodePort :30900]
            NODE_POD[Kubelet metrics<br/>:10250]
            CAD[CAdvisor<br/>:10255]
            WORKLOADS[Workload Pods]
        end

        PROM_POD -->|scrape metrics| NODE_POD
        PROM_POD -->|scrape metrics| CAD
        PROM_POD -->|scrape metrics| WORKLOADS
    end

    EB -->|trigger| Lambda
    Lambda -->|read/write| DDB1
    Lambda -->|append| DDB2
    Lambda -->|fetch token| S3
    Lambda -->|HTTP Query :30900| PROM_POD
    Lambda -->|emit| CW

    Lambda -->|scale decision| EC2[(EC2 API)]
    EC2 -->|launch/terminate| Wdyn

    Master <-->|manage| K8s
    W1 <-->|join| K8s
    W2 <-->|join| K8s
    Wdyn <-->|join| K8s

    K8s -->|schedule| PROM_POD
    K8s -->|schedule| WORKLOADS

    CW -->|threshold| ALRM

    style W1 fill:#90EE90
    style W2 fill:#90EE90
    style Wdyn fill:#FFE4B5
    style PROM_POD fill:#FF6B6B
```

## Autoscaler Decision Flow

```mermaid
flowchart TD
    START([EventBridge Trigger<br/>Every 2min]) --> INIT[Initialize Lambda Context]

    INIT --> ACQUIRE[Acquire Distributed Lock<br/>DynamoDB ConditionalWrite]

    ACQUIRE -->|lock failed| EXIT1([Exit: Another run in progress])
    ACQUIRE -->|lock acquired| FETCH_STATE[Fetch Cluster State<br/>DynamoDB]

    FETCH_STATE --> CHECK_COOLDOWN{In Cooldown?}
    CHECK_COOLDOWN -->|yes| RELEASE[Release Lock] --> EXIT2([Exit: Cooldown active])
    CHECK_COOLDOWN -->|no| QUERY_METRICS[Query Prometheus<br/>CPU, Memory, Pending Pods]

    QUERY_METRICS --> CALC_DECISION[Calculate Scaling Decision]

    CALC_DECISION --> DECISION{Which Action?}

    DECISION -->|Scale Up| SCALE_UP[Scale Up Flow]
    DECISION -->|Scale Down| SCALE_DOWN[Scale Down Flow]
    DECISION -->|No Change| NO_OP[No Operation]

    SCALE_UP --> CHECK_MAX{At Max Nodes?}
    CHECK_MAX -->|yes| NO_OP
    CHECK_MAX -->|no| LAUNCH[Launch New EC2 Instance]

    LAUNCH --> RECORD_WAL_UP[Record WAL Entry:<br/>STARTED]
    RECORD_WAL_UP --> TAG_EC2[Tag EC2:<br/>NodeRole=worker]
    TAG_EC2 --> WAIT_READY[Wait for Node Ready<br/>kubectl get nodes]
    WAIT_READY --> JOIN_CLUSTER[Node Joins Cluster]
    JOIN_CLUSTER --> COMPLETE_WAL_UP[Complete WAL:<br/>SUCCEEDED]
    COMPLETE_WAL_UP --> SET_COOLDOWN_UP[Set Scale-Up Cooldown<br/>5min]

    SCALE_DOWN --> CHECK_MIN{At Min Nodes?}
    CHECK_MIN -->|yes| NO_OP
    CHECK_MIN -->|no| SELECT_NODE[Select Node to Remove<br/>LIFO: Exclude Permanent]

    SELECT_NODE --> CORDON[Cordon & Drain Node<br/>kubectl drain]
    CORDON --> RECORD_WAL_DOWN[Record WAL Entry:<br/>STARTED]
    RECORD_WAL_DOWN --> TERMINATE[Terminate EC2 Instance]
    TERMINATE --> COMPLETE_WAL_DOWN[Complete WAL:<br/>SUCCEEDED]
    COMPLETE_WAL_DOWN --> SET_COOLDOWN_DOWN[Set Scale-Down Cooldown<br/>15min]

    SET_COOLDOWN_UP --> UPDATE_STATE[Update Cluster State<br/>Node count, timestamp]
    SET_COOLDOWN_DOWN --> UPDATE_STATE
    NO_OP --> UPDATE_STATE

    UPDATE_STATE --> RELEASE_LOCK[Release Distributed Lock]
    RELEASE_LOCK --> EMIT_METRICS[Emit CloudWatch Metrics]
    EMIT_METRICS --> EXIT3([Exit Complete])

    PROM[Prometheus] -.->|HTTP Query| QUERY_METRICS
    EC2_API[EC2 API] -.->|RunInstances| LAUNCH
    EC2_API -.->|TerminateInstances| TERMINATE
    K8s_API[K8s API] -.->|Drain| CORDON

    style SCALE_UP fill:#FF6B6B
    style SCALE_DOWN fill:#4ECDC4
    style NO_OP fill:#95E1D3
```

## Lambda Internal Architecture

```mermaid
graph TB
    subgraph "Lambda Handler"
        Handler[lambda_handler.py]

        subgraph "Core Modules"
            Metrics[metrics.py<br/>Prometheus Client]
            State[state.py<br/>DynamoDB Client]
            Scaling[scaling.py<br/>Decision Engine]
            EC2[ec2_operations.py<br/>AWS SDK]
        end

        subgraph "Utilities"
            Lock[lock.py<br/>Distributed Lock]
            WAL[wal.py<br/>Write-Ahead Log]
            Config[config.py<br/>Environment Vars]
        end
    end

    Handler --> Metrics
    Handler --> State
    Handler --> Scaling
    Handler --> EC2

    Metrics --> PROM_EP[HTTP: NodePort 30900<br/>Prometheus in K3s Cluster]
    State --> DDB
    EC2 --> EC2_API

    Handler --> Lock
    Handler --> WAL
    Lock --> DDB
    WAL --> DDB

    Handler --> Config
    Config --> ENV[Environment Variables]

    K3S[('K3s Cluster')] -.->|hosts| PROM_EP
```

## Scaling Decision Logic

```mermaid
flowchart LR
    subgraph "Metrics"
        CPU[CPU %]
        MEM[Memory %]
        PEND["Pending Pods"]
    end

    subgraph "Thresholds"
        CPU_UP[> 70%]
        CPU_DOWN[< 30%]
        MEM_UP[> 70%]
        MEM_DOWN[< 50%]
        PEND_THRESH[>= 1]
    end

    subgraph "Decision"
        OR1{{Scale Up Trigger}}
        AND1{{Scale Down Trigger}}
    end

    CPU --> CPU_UP
    MEM --> MEM_UP
    MEM --> MEM_DOWN
    PEND --> PEND_THRESH

    CPU_UP --> OR1
    PEND_THRESH --> OR1

    CPU_DOWN --> AND1
    MEM_DOWN --> AND1

    OR1 -->|TRUE| UP[SCALE UP]
    AND1 -->|TRUE| DOWN[SCALE DOWN]

    OR1 -->|FALSE| CHECK1{Check Down}
    AND1 -->|FALSE| CHECK2{Check Up}

    CHECK1 -->|AND1=TRUE| DOWN
    CHECK2 -->|OR1=TRUE| UP

    CHECK1 -->|FALSE| NOP[NO CHANGE]
    CHECK2 -->|FALSE| NOP

    style UP fill:#FF6B6B
    style DOWN fill:#4ECDC4
    style NOP fill:#95E1D3
```

## State Management (DynamoDB)

```mermaid
graph TB
    subgraph "Cluster State Table"
        CS_KEY[PK: cluster_id]
        CS_ATTRS[Attributes:<br/>- node_count<br/>- scaling_in_progress<br/>- last_scale_time<br/>- current_cooldown<br/>- ttl]
    end

    subgraph "WAL Table"
        WAL_KEY[PK: operation_id<br/>RK: started_at]
        WAL_ATTRS[Attributes:<br/>- state<br/>- operation_type<br/>- node_id<br/>- error_message<br/>- ttl]
    end

    subgraph "GSI: ScalingStatusIndex"
        GSI_HASH[hash: scaling_in_progress]
        GSI_RANGE[range: last_scale_time]
    end

    subgraph "GSI: IncompleteOperations"
        GSI2_HASH[hash: state]
        GSI2_RANGE[range: started_at]
    end

    CS_ATTRS --> GSI_HASH
    CS_ATTRS --> GSI_RANGE
    WAL_ATTRS --> GSI2_HASH
    WAL_ATTRS --> GSI2_RANGE
```

## EC2 Instance Lifecycle

```mermaid
stateDiagram-v2
    [*] --> Pending: Lambda Launch Request
    Pending --> Running: EC2 Instance Running
    Running --> Joining: K3s Agent Start
    Joining --> Ready: kubectl get nodes Ready
    Ready --> Active: Mark Active in State
    Active --> Cordoned: Scale Down Selected
    Cordoned --> Draining: kubectl drain
    Draining --> Terminated: EC2 Terminate
    Terminated --> [*]: WAL Complete

    note right of Pending
        tags: NodeRole=worker
        CreatedBy=autoscaler
    end note

    note right of Active
        Accepts pods
        Metrics collected
    end note

    note right of Cordoned
        No new pods
        Existing pods drained
    end note
```

## Error Handling & Recovery

```mermaid
flowchart TD
    START([Lambda Start]) --> TRY[Try: Acquire Lock]

    TRY -->|Exception| CATCH["Catch Exception"]

    subgraph "Error Handling"
        CATCH --> LOG_ERROR["Log to CloudWatch"]
        LOG_ERROR --> RECORD_WAL[Record WAL: FAILED]
        RECORD_WAL --> CHECK_RETRY{Retryable?}
        CHECK_RETRY -->|yes| CALC_BACKOFF["Calculate Backoff"]
        CHECK_RETRY -->|no| ALERT["Send CloudWatch Alarm"]

        CALC_BACKOFF --> SCHEDULE_RETRY[Schedule Retry<br/>Next EventBridge]
    end

    SCHEDULE_RETRY --> RELEASE[Release Lock]
    ALERT --> RELEASE

    TRY -->|Success| MAIN[Main Logic]
    MAIN --> RELEASE
    RELEASE --> END([Lambda End])

    RELEASE -->|Force| FORCE_RELEASE[Conditional Delete<br/>Lock TTL]
```

## Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `CHECK_INTERVAL` | 120s | EventBridge schedule |
| `SCALE_UP_THRESHOLD` | 70% | CPU threshold for scale-up |
| `SCALE_DOWN_THRESHOLD` | 30% | CPU threshold for scale-down |
| `SCALE_UP_COOLDOWN` | 300s | Wait after scale-up |
| `SCALE_DOWN_COOLDOWN` | 900s | Wait after scale-down |
| `MIN_NODES` | 2 | Minimum cluster size |
| `MAX_NODES` | 10 | Maximum cluster size |
| `NODE readiness_TIMEOUT` | 300s | Max wait for node ready |
| `PROMETHEUS_URL` | - | Prometheus NodePort: `http://<any-worker-ip>:30900` |
| `K3S_MASTER_URL` | - | Kubernetes API endpoint for drain operations |

**Note**: Prometheus runs as a pod in the K3s cluster. Lambda connects via NodePort service on port 30900 (accessible on any worker node IP).

## Data Flows

### Scale Up Flow
1. EventBridge triggers Lambda
2. Lambda acquires distributed lock (DynamoDB)
3. Query Prometheus: CPU > 70% OR pending_pods >= 1?
4. Check cooldown period expired
5. Check current nodes < MAX_NODES
6. **EC2.RunInstances()** with AMI, subnet, security group, IAM profile
7. Create WAL entry: state=STARTED
8. Wait for node: poll kubectl get nodes
9. Node Ready → Update WAL: state=SUCCEEDED
10. Update cluster state: node_count++, last_scale_time
11. Set scale-up cooldown (5 min)
12. Release lock

### Scale Down Flow
1. EventBridge triggers Lambda
2. Lambda acquires distributed lock
3. Query Prometheus: CPU < 30% AND Memory < 50%?
4. Check cooldown expired
5. Check current nodes > MIN_NODES
6. Select node: LIFO, exclude `Permanent=true`
7. **kubectl drain** node (evict pods)
8. Create WAL entry: state=STARTED
9. **EC2.TerminateInstances()**
10. Update WAL: state=SUCCEEDED
11. Update cluster state: node_count--, last_scale_time
12. Set scale-down cooldown (15 min)
13. Release lock