import sys
import json
import urllib.request
import urllib.error
import time
import os

# ================= 配置区域 =================
# 注意：地址不要带最后的斜杠
SOURCE_REGISTRY = "http://127.0.0.1:5001"
TARGET_REGISTRY = "http://127.0.0.1:5002"

# 要同步的镜像列表 (格式: "镜像名:Tag")
IMAGES_TO_SYNC = [
    "alpine:latest"
]
# ===========================================

# Docker V2 API Headers
MANIFEST_HEADERS = {
    "Accept": "application/vnd.docker.distribution.manifest.v2+json"
}

def request(method, url, headers={}, data=None):
    """封装 urllib 请求"""
    req = urllib.request.Request(url, headers=headers, method=method)
    if data:
        req.data = data
    return urllib.request.urlopen(req)

def check_blob_exists(registry, repo, digest):
    """利用 HEAD 请求检测 Blob 是否存在 (极省带宽)"""
    url = f"{registry}/v2/{repo}/blobs/{digest}"
    try:
        request("HEAD", url)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise e

class ProgressReader:
    def __init__(self, response, total_size):
        self.response = response
        self.total_size = total_size
        self.bytes_read = 0
        self.start_time = time.time()
        self.last_time = self.start_time
        self.last_bytes = 0

    def read(self, size):
        chunk = self.response.read(size)
        if chunk:
            self.bytes_read += len(chunk)
            self._print_progress()
        return chunk

    def __iter__(self):
        # urllib/http.client iterates over the data if it's an iterable
        while True:
            chunk = self.read(8192 * 4) # 32KB chunks
            if not chunk:
                break
            yield chunk

    def _print_progress(self):
        current_time = time.time()
        # Update every 0.1s or if finished
        if current_time - self.last_time < 0.1 and self.bytes_read < self.total_size:
            return

        elapsed = current_time - self.start_time
        speed = self.bytes_read / elapsed if elapsed > 0 else 0
        
        # Format size
        def fmt_size(b):
            if b < 1024: return f"{b}B"
            if b < 1024*1024: return f"{b/1024:.1f}KB"
            return f"{b/(1024*1024):.1f}MB"

        percent = 100.0 * self.bytes_read / self.total_size if self.total_size > 0 else 0
        bar_len = 30
        filled = int(bar_len * percent / 100)
        bar = "=" * filled + ">" + " " * (bar_len - filled - 1)
        
        # Clear line and print
        sys.stdout.write(f"\r    [{bar}] {percent:.1f}% {fmt_size(self.bytes_read)}/{fmt_size(self.total_size)} {fmt_size(speed)}/s")
        sys.stdout.flush()
        
        self.last_time = current_time

def stream_upload_blob(repo, digest):
    """核心：从源读取流，直接写入目标 (流式传输，不占内存/硬盘)"""
    # 1. 从源获取下载流
    src_url = f"{SOURCE_REGISTRY}/v2/{repo}/blobs/{digest}"
    try:
        src_resp = request("GET", src_url)
        total_size = int(src_resp.headers.get("Content-Length", 0))
    except urllib.error.HTTPError as e:
        print(f"  [ERROR] Source missing blob {digest}")
        return False

    # 2. 在目标初始化上传
    init_url = f"{TARGET_REGISTRY}/v2/{repo}/blobs/uploads/"
    try:
        init_resp = request("POST", init_url, headers={"Content-Length": "0"})
        upload_location = init_resp.headers.get("Location")
    except Exception as e:
        print(f"  [ERROR] Init upload failed: {e}")
        return False

    # 3. 管道传输：Source Read -> Target Write
    print(f"    -> Transferring {digest[:12]}...", end="", flush=True)
    
    # 重新构造 PUT 请求
    if "?" in upload_location:
        upload_url = f"{upload_location}&digest={digest}"
    else:
        upload_url = f"{upload_location}?digest={digest}"

    # 使用 ProgressReader 包装 response
    # 注意：urllib 如果接收 iterable data，会使用 chunked encoding，除非指定 Content-Length
    # 我们这里指定 Content-Length，让它知道总长度
    reader = ProgressReader(src_resp, total_size)
    
    req = urllib.request.Request(upload_url, data=reader, method="PUT")
    req.add_header("Content-Type", "application/octet-stream")
    if total_size > 0:
        req.add_header("Content-Length", str(total_size))
    
    try:
        urllib.request.urlopen(req)
        print("\n    Done.") # Newline after progress bar
        return True
    except Exception as e:
        print(f"\n    Failed: {e}")
        return False

def sync_image(image_tag):
    repo, tag = image_tag.split(":")
    print(f"\n[SYNC] Processing {image_tag} ...")

    # 1. 获取源 Manifest
    try:
        url = f"{SOURCE_REGISTRY}/v2/{repo}/manifests/{tag}"
        resp = request("GET", url, headers=MANIFEST_HEADERS)
        manifest_data = resp.read()
        manifest = json.loads(manifest_data)
    except Exception as e:
        print(f"  [ERROR] Could not get manifest from source: {e}")
        return

    # 2. 遍历所有 Layer (包括 Config 和 Layers)
    # 构造需要检查的 digest 列表
    if 'layers' in manifest:
        blobs = [manifest['config']] + manifest['layers']
    elif 'fsLayers' in manifest: # Handle v1 schema if necessary, though headers req v2
         print("  [ERROR] Received V1 manifest, script only supports V2 Schema 2.")
         return
    else:
         print(f"  [ERROR] Unknown manifest format: {manifest.keys()}")
         return

    for blob in blobs:
        digest = blob['digest']
        # --- 关键步骤：利用 API 检查目标是否存在 ---
        if check_blob_exists(TARGET_REGISTRY, repo, digest):
            print(f"  [SKIP] Layer {digest[:12]} exists on target.")
        else:
            print(f"  [MISS] Layer {digest[:12]} missing.")
            success = stream_upload_blob(repo, digest)
            if not success:
                print("  [ABORT] Blob transfer failed.")
                return

    # 3. 最后推送 Manifest (这一步会让镜像在目标端可见)
    print("  [META] Pushing Manifest...")
    put_url = f"{TARGET_REGISTRY}/v2/{repo}/manifests/{tag}"
    req = urllib.request.Request(put_url, data=manifest_data, method="PUT")
    req.add_header("Content-Type", "application/vnd.docker.distribution.manifest.v2+json")
    try:
        urllib.request.urlopen(req)
        print(f"  [SUCCESS] {image_tag} synced successfully!")
    except Exception as e:
        print(f"  [ERROR] Manifest push failed: {e}")

if __name__ == "__main__":
    if len(IMAGES_TO_SYNC) == 0:
        print("Please configure IMAGES_TO_SYNC list in the script.")
    
    for img in IMAGES_TO_SYNC:
        sync_image(img)
