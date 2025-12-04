# Air-Gap Docker Registry Sync

这是一个轻量级的 Python 脚本，利用 Docker Registry HTTP API V2 在两个 Docker 镜像仓库（Registry）之间同步镜像。

**核心优势：**
*   **无需 Docker Daemon**：运行此脚本的机器**不需要**安装 Docker 客户端或守护进程。
*   **零依赖**：仅使用 Python 标准库 (`urllib`, `json`, `sys`)，无需 `pip install` 任何第三方库。非常适合无法连接外网的隔离环境。
*   **流式传输 (Streaming)**：直接将数据流从源仓库管道传输到目标仓库。**不占用本地磁盘空间**，内存占用极低（仅需少量缓冲区）。
*   **智能跳过**：自动检测目标仓库是否存在相同的 Layer (Blob)，避免重复传输，节省带宽。

## 适用场景

1.  **Air-Gapped (物理隔离) 环境**：在只有网络连通但无法安装复杂依赖的堡垒机上运行，将外网/DMZ 仓库的镜像搬运到内网仓库。
2.  **CI/CD 流水线**：在没有 Docker Socket 权限的容器内同步镜像。
3.  **跨地域同步**：低带宽环境下高效同步基础镜像。

## 环境要求

*   Python 3.6+
*   源仓库和目标仓库的网络访问权限

## 配置方法

打开 `sync_docker_images.py` 文件，修改顶部的 **配置区域**：

```python
# ================= 配置区域 =================
# 设置源仓库和目标仓库地址 (注意：不要带最后的斜杠)
SOURCE_REGISTRY = "http://192.168.1.10:5000"
TARGET_REGISTRY = "http://192.168.1.20:5000"

# 要同步的镜像列表 (格式: "镜像名:Tag")
IMAGES_TO_SYNC = [
    "ubuntu:latest",
    "nginx:alpine",
    "my-app:v1"
]
# ===========================================
```

## 使用方法

直接运行脚本即可：

```bash
python3 sync_docker_images.py
```

### 运行输出示例

```text
[SYNC] Processing alpine:latest ...
  [MISS] Layer sha256:8764b missing.
    -> Transferring sha256:8764b... Done.
  [SKIP] Layer sha256:0bd71 exists on target.
  [META] Pushing Manifest...
  [SUCCESS] alpine:latest synced successfully!
```

## 工作原理

脚本严格遵循 [Docker Registry HTTP API V2](https://docs.docker.com/registry/spec/api/) 标准：

1.  **获取清单 (Get Manifest)**：从源仓库下载指定 Tag 的 Manifest V2 JSON。
2.  **解析层 (Parse Layers)**：提取 Config 和 Layers 的 Digest (哈希值)。
3.  **检查存在性 (Check Existence)**：对目标仓库发起 `HEAD` 请求，检查 Blob 是否已存在。
4.  **管道流传输 (Stream Blob)**：
    *   如果 Blob 缺失，从源发起 `GET` 请求获取读取流。
    *   在目标发起 `POST` (init upload) 和 `PUT` (commit upload) 请求。
    *   数据通过内存管道直接转发，不落地磁盘。
5.  **推送清单 (Put Manifest)**：所有 Layer 传输完成后，将 Manifest 推送到目标仓库，完成镜像注册。

## 注意事项与限制

1.  **认证 (Authentication)**：
    *   当前版本代码**仅支持免密访问**的仓库（如内部私有仓库）。
    *   如果仓库需要 Basic Auth 或 Token Auth，需要修改 `request` 函数添加 `Authorization` 头。
2.  **HTTPS 证书**：
    *   如果使用的是自签名证书的 HTTPS 仓库，Python 可能会报错 SSL 验证失败。可以通过导入 `ssl` 模块并修改 `urllib.request.urlopen` 的 `context` 参数来忽略证书验证（生产环境不推荐）。
3.  **Manifest 格式**：
    *   脚本主要支持 `application/vnd.docker.distribution.manifest.v2+json` (Schema 2)。对于极老的 Schema 1 镜像可能不兼容。
