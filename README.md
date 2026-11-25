# kubelet-pod-garbage-collection-validator

使用 Python 3.8 兼容语法离线模拟 kubelet 的 Pod GC 判定：根据调用方提供的
快照数据计算哪些资源可以清理、哪些必须保留、哪些属于卷泄漏。

## 不连接 kubelet

本项目是一个**纯离线、纯函数**的验证器：

- 不连接 kubelet、Kubernetes API Server 或容器运行时（CRI）；
- 不删除任何文件、镜像或容器，也不会访问输入中出现的目录路径；
- 所有输入均由调用方以内存对象或 JSON 快照提供，输出只是一份决策报告；
- 仅依赖 Python 标准库（`dataclasses`、`enum`、`argparse`、`json`、`unittest`）。

## 输入字段

每个 Pod 快照包含题目要求的全部输入：

| 输入 | 字段 | 说明 |
| --- | --- | --- |
| Pod UID | `uid` | Pod 唯一标识，同时用于卷引用匹配 |
| 容器状态 | `containers[].state` | `running` / `waiting` / `terminated` / `unknown` |
| 卷引用 | `volumes[].referenced_by` | 仍挂载该卷的全部 Pod UID，用于判断镜像卷共享 |
| 镜像 | `containers[].image` | 镜像 GC 时统计仍在使用的引用 |
| 静态标记 | `is_static` | 是否为静态 Pod |
| mirror Pod | `mirror_pod_exists` / `mirror_pod_uid` | mirror Pod 是否仍存在 |
| 孤儿目录 | `orphan_directories[].path` | 磁盘上发现、但配置中无属主的目录 |
| 删除时间 | `deletion_timestamp` | ISO 8601 字符串，仅已终止且带删除时间的 Pod 才可能被清理 |

字典格式示例见 `examples/snapshot.json`，可用
`python -m pod_gc.cli examples/snapshot.json` 直接生成报告。

## 判定规则

核心实现位于 `pod_gc/gc.py`，强制执行三条安全规则：

1. **仍有卷引用的目录不能删。** 卷的 `referenced_by` 中存在其他存活 Pod
   （典型场景：已终止 Pod 的镜像卷被新 Pod 挂载）时，卷目录及其所属 Pod
   目录都进入保留列表，原因记为 `volume_still_referenced`。
2. **未知状态不能按已终止处理。** 任一容器为 `unknown` 时，Pod 一律保留，
   原因记为 `unknown_container_state`，其镜像也不会被当作无引用镜像回收。
3. **静态 Pod 不能因 mirror 消失而终止。** `is_static=True` 且
   `mirror_pod_exists=False` 时保留 Pod，原因记为
   `static_pod_mirror_missing`；mirror 缺失只作为证据，不是终止信号。

其余规则：

- 所有容器 `terminated` **且** `deletion_timestamp` 非空，且无共享卷时，
  Pod 目录与无引用卷目录才进入可清理列表，报告中保留删除时间。
- 镜像仅被可清理 Pod 引用时才可回收；仍被保留 Pod 引用则保留
  （`image_still_in_use`）。
- 孤儿卷目录若仍被存活 Pod 引用则保留；若既无属主 Pod 又无任何引用，
  记入**卷泄漏**（`volume_leaks`）而不是静默删除，便于人工核查。
- 非卷形态、无属主的孤儿目录保留为证据，不自动清理。

## 输出

`evaluate_garbage_collection(pods, orphan_directories)` 返回 `GCReport`：

- `cleanable`：可清理项（`pod_directory` / `volume_directory` / `image`）；
- `retained`：保留证据，每项包含 `reason` 与可读 `evidence`；
- `volume_leaks`：无属主、无引用的卷目录泄漏清单。

## 测试

测试位于 `tests/test_pod_gc.py`，覆盖题目要求的三类场景：

- **卷共享**：镜像卷被新 Pod 引用时旧 Pod 目录与卷目录均不可清理，
  共享镜像同样保留；无引用卷可正常清理；
- **mirror 删除**：静态 Pod 在 mirror Pod 消失后仍保留，mirror 存在时
  按普通规则判定；
- **容器状态丢失**：`unknown` 状态不视为终止，运行中与缺少删除时间的
  Pod 同样保留；
- 另含孤儿卷泄漏、被引用孤儿目录保留、JSON 快照解析等用例。

运行方式：

```bash
python -m unittest
```

代码仅使用 Python 3.8.2 已支持的语法与标准库 API（如 `datetime.fromisoformat`）。
