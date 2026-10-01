# 内容来源验证服务

可供其他应用调用的独立来源验证 API。服务只接收单张 JPEG、PNG 或 WebP 图片；图片字节在内存中处理，不写入临时文件。日志不包含请求体、密钥或异常文本。

## 运行

Python 3.10+：

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export SERVICE_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
uvicorn app:app --host 0.0.0.0 --port 808 --no-access-log
```

## Docker + Nginx 部署示例

以下仅是通用示例，不假定特定云主机、SSH 端口、域名或面板。API 容器内部监听 `808`；示例将宿主机的 `127.0.0.1:18086` 转发到容器 `808`，该端口可按部署环境调整。将 `deploy/nginx-provenance.conf` 作为 `location` 配置片段放进你自己的 HTTPS `server` 配置中，并按需修改路径和上游端口。`proxy_pass` 地址末尾的 `/` 会剥离 `/provenance/` 前缀再转发。

```sh
docker build -t provenance-verification:latest .
docker run -d --name provenance-verification \
  --restart unless-stopped \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
  --security-opt no-new-privileges:true \
  --cap-drop ALL \
  --publish 127.0.0.1:18086:808 \
  --env-file /etc/provenance-verification/service.env \
  provenance-verification:latest
```

先在仓库外创建 `/etc/provenance-verification/service.env`，设置至少 32 字符的随机 `SERVICE_API_TOKEN`，并限制文件权限为 `0600`。不要把环境文件加入仓库。OpenAI key 不放服务端环境文件，而是按请求通过 `X-OpenAI-API-Key` 传入。只向公网开放 HTTPS 反向代理端口，不要公开宿主机上的 API 上游端口；确认代理及其日志系统不会记录该请求头。

`/health` 是无认证健康检查，其余验证接口需要 bearer token。Uvicorn access log 已关闭；反向代理日志策略由部署者配置。

## API

请求体是图片原始字节（不是 multipart），类型由 `Content-Type` 指定：

```sh
curl -H "Authorization: Bearer $SERVICE_API_TOKEN" \
  -H "X-OpenAI-API-Key: $OPENAI_API_KEY" \
  -H 'Content-Type: image/jpeg' --data-binary @photo.jpg \
  'https://<your-domain>/provenance/v1/verify?include_openai=true'
```

`include_openai=true` 强制执行检查；`openai_fallback=true` 仅在没有“已验证且带签发者”的可信 C2PA 凭据时执行。模式启用且请求包含 `X-OpenAI-API-Key` 时，服务会将图片发送到 OpenAI 官方 `POST /v1/content_provenance_checks` 接口。OpenAI key 只存在于当前请求和处理内存中，不读取服务端环境变量，也不保存到磁盘或返回结果。请使用 HTTPS，并确认反向代理不会记录 `X-OpenAI-API-Key` 请求头。服务不会自行重试外部请求。OpenAI 来源验证不是通用 AI 图像检测器，也不能识别其他厂商生成内容。

返回的 `status` 仅使用：

- `verified_source`：C2PA SDK 的验证状态为 `Trusted`，表示声明签名通过且签发者处于 SDK 信任状态；这本身不代表图片一定是 AI 生成。
- `unverified_claim_found`：发现 C2PA 声明但签发者尚未建立信任或声明无效，或 OpenAI API 检出支持信号。
- `no_supported_signal_found`：当前已执行检查未发现支持信号，不能解读为“不是 AI”。
- `detection_failed_or_unsupported`：解析、网络、配置或外部 API 检查失败/不可用。

每次上传最多 10 MiB（可通过环境变量调整，上限 50 MiB），每 IP 默认每分钟 10 次。C2PA 检查在工作线程运行并受超时约束；OpenAI HTTP 请求有连接和总超时。

## 本地验证

```sh
python3 -m unittest discover -s tests -v
```
