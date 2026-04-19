# ptgen

支持三种使用方式：

1. **命令行（原有）**
   ```bash
   python ptgen.py tt0133093
   # 或
   python ptgen.py https://movie.douban.com/subject/1291843/
   ```

2. **Web 端**
   ```bash
   python ptgen.py --serve
   ```
   默认监听 `127.0.0.1:8000`，浏览器打开：`http://127.0.0.1:8000/`

3. **API 调用**
   - GET:
     ```bash
     curl 'http://127.0.0.1:8000/api/generate?input=tt0133093'
     ```
   - POST JSON:
     ```bash
     curl -X POST 'http://127.0.0.1:8000/api/generate' \
       -H 'Content-Type: application/json' \
       -d '{"input":"tt0133093"}'
     ```

返回示例：
```json
{
  "ok": true,
  "input": "tt0133093",
  "result": "[img]...[/img]\n◎译　　名 ..."
}
```
