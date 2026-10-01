# 内容来源验证服务

面向 219 小主机调用的独立验证 API。服务只接收单张 JPEG、PNG 或 WebP 图片；图片字节在内存中处理，不写入临时文件。日志不包含请求体、密钥或异常文本。

## 运行

Python 3.10+：

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export SERVICE_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
uvicorn app:app --host 0.0.0.0 --port 808 --no-access-log
```

## 海外机宝塔部署

该服务器的 SSH 使用 808 端口，因此 API 容器内部监听 808，宿主机只映射到 `127.0.0.1:18086`，不占用 SSH 端口。使用宝塔已管理的 Docker 和 Nginx：将 `deploy/baota-provenance.conf` 安装到 `api.567.wiki` 的 Nginx 扩展配置目录后，外部 HTTPS 路径为 `https://api.567.wiki/provenance/`。反向代理会去掉 `/provenance/` 前缀再转发。

服务容器使用只读根文件系统及仅供临时运行使用的 `/tmp`，并关闭 Uvicorn 请求访问日志。`SERVICE_API_TOKEN` 放在服务器仓库目录之外、权限为 `0600` 的环境文件中；OpenAI key 不放服务器环境文件，按请求从 `X-OpenAI-API-Key` 传入。不要将环境文件加入仓库或宝塔网站根目录。

生产环境请将密钥放在服务管理器的环境文件中（权限 `0600`），不要放进仓库；公网部署应由 TLS 反向代理终止 HTTPS，并将端口 808 限制为受信网络可访问。Uvicorn access log 已关闭，以免记录查询参数。`/health` 是无认证健康检查，其余验证接口需要 bearer token。

## API

请求体是图片原始字节（不是 multipart），类型由 `Content-Type` 指定：

```sh
curl -H "Authorization: Bearer $SERVICE_API_TOKEN" \
  -H "X-OpenAI-API-Key: $OPENAI_API_KEY" \
  -H 'Content-Type: image/jpeg' --data-binary @photo.jpg \
  'http://127.0.0.1:808/v1/verify?include_openai=true'
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
