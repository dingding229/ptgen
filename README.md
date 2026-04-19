# ptgen

一个可同时提供 **CLI / Web / API / Admin** 的影视简介生成工具。

## 启动方式

### 1) CLI
```bash
python ptgen.py tt0133093
# 或
python ptgen.py https://movie.douban.com/subject/1291843/
```

### 2) Web + API + 管理后台
```bash
# 默认监听 0.0.0.0:53000（可公网访问）
python ptgen.py --serve

# 自定义 host/port
python ptgen.py --serve 0.0.0.0 53000
```

访问：
- Web 页面：`http://<服务器IP>:53000/`
- API：`http://<服务器IP>:53000/api/generate`
- 管理后台：`http://<服务器IP>:53000/admin`

## API 调用

- GET:
```bash
curl 'http://127.0.0.1:53000/api/generate?input=tt0133093'
```

- POST JSON:
```bash
curl -X POST 'http://127.0.0.1:53000/api/generate' \
  -H 'Content-Type: application/json' \
  -d '{"input":"tt0133093"}'
```

返回示例：
```json
{
  "ok": true,
  "input": "tt0133093",
  "result": "[img]...[/img]\n◎译　　名 ...",
  "duration_ms": 1234
}
```

## 管理后台功能

管理后台默认密码来源于环境变量 `PTGEN_ADMIN_PASSWORD`：

```bash
export PTGEN_ADMIN_PASSWORD='your_strong_password'
python ptgen.py --serve
```

后台功能：
- 登录 / 退出
- 缓存统计
- 缓存清理
- 最近请求历史（含成功/失败、耗时）

## 公网访问注意事项

如果你使用公网 IP 仍无法访问，请检查：
- 云服务器安全组是否放行 `53000/TCP`
- 系统防火墙（如 `ufw` / `firewalld`）是否放行 `53000`
- 是否使用 `0.0.0.0` 监听（默认已开启）
