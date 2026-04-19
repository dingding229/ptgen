package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"embed"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io/fs"
	"log"
	"net/http"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

//go:embed web/*
var webFS embed.FS

type App struct {
	dbPath     string
	jwtSecret  []byte
	cacheTTL   time.Duration
	adminUser  string
	adminPass  string
	issuerName string
	mu         sync.Mutex
}

type jsonMap map[string]any

func main() {
	host := flag.String("host", envOr("PTGEN_HOST", "0.0.0.0"), "listen host")
	port := flag.Int("port", envInt("PTGEN_PORT", 53000), "listen port")
	dbPath := flag.String("db", envOr("PTGEN_DB", "./data/ptgen.db"), "sqlite db path")
	flag.Parse()

	if err := os.MkdirAll("./data", 0o755); err != nil {
		log.Fatalf("create data dir: %v", err)
	}

	app := &App{
		dbPath:     *dbPath,
		jwtSecret:  []byte(envOr("PTGEN_JWT_SECRET", "change-this-in-production")),
		cacheTTL:   10 * time.Minute,
		adminUser:  envOr("PTGEN_ADMIN_USER", "admin"),
		adminPass:  envOr("PTGEN_ADMIN_PASSWORD", "admin123"),
		issuerName: "ptgen-go",
	}

	if err := app.initDB(); err != nil {
		log.Fatalf("init db: %v", err)
	}
	if err := app.ensureAdmin(); err != nil {
		log.Fatalf("ensure admin: %v", err)
	}

	mux := http.NewServeMux()
	app.routes(mux)

	addr := fmt.Sprintf("%s:%d", *host, *port)
	log.Printf("ptgen-go listening on http://%s", addr)
	log.Printf("web: http://%s/", addr)
	log.Printf("admin: http://%s/admin", addr)
	if *host == "0.0.0.0" {
		log.Printf("public access: http://<your-public-ip>:%d", *port)
	}

	if err := http.ListenAndServe(addr, loggingMiddleware(mux)); err != nil {
		log.Fatal(err)
	}
}

func (a *App) routes(mux *http.ServeMux) {
	sub, _ := fs.Sub(webFS, "web")
	mux.Handle("/assets/", http.StripPrefix("/assets/", http.FileServer(http.FS(sub))))
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		http.ServeFileFS(w, r, sub, "index.html")
	})
	mux.HandleFunc("/admin", func(w http.ResponseWriter, r *http.Request) {
		http.ServeFileFS(w, r, sub, "admin.html")
	})

	mux.HandleFunc("/api/generate", a.handleGenerate)
	mux.HandleFunc("/api/admin/login", a.handleAdminLogin)
	mux.HandleFunc("/api/admin/stats", a.auth(a.handleAdminStats))
	mux.HandleFunc("/api/admin/history", a.auth(a.handleAdminHistory))
	mux.HandleFunc("/api/admin/cache/clear", a.auth(a.handleAdminCacheClear))
}

func (a *App) sqliteExec(sql string) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	cmd := exec.Command("sqlite3", a.dbPath, sql)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("sqlite exec error: %v, %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

func (a *App) sqliteJSON(sql string) ([]map[string]any, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	cmd := exec.Command("sqlite3", "-json", a.dbPath, sql)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return nil, fmt.Errorf("sqlite query error: %v, %s", err, strings.TrimSpace(string(out)))
	}
	trimmed := strings.TrimSpace(string(out))
	if trimmed == "" {
		return []map[string]any{}, nil
	}
	var rows []map[string]any
	if err := json.Unmarshal(out, &rows); err != nil {
		return nil, err
	}
	return rows, nil
}

func q(s string) string {
	return "'" + strings.ReplaceAll(s, "'", "''") + "'"
}

func (a *App) initDB() error {
	schema := `
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  input TEXT NOT NULL,
  ok INTEGER NOT NULL,
  result TEXT,
  error TEXT,
  source TEXT NOT NULL,
  duration_ms INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cache (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);`
	return a.sqliteExec(schema)
}

func passHash(password string) string {
	h := sha256.Sum256([]byte(password))
	return hex.EncodeToString(h[:])
}

func (a *App) ensureAdmin() error {
	rows, err := a.sqliteJSON(`SELECT COUNT(1) AS cnt FROM users WHERE username = ` + q(a.adminUser))
	if err != nil {
		return err
	}
	if len(rows) > 0 {
		if cnt, _ := toInt(rows[0]["cnt"]); cnt > 0 {
			return nil
		}
	}
	sql := `INSERT INTO users (username, password_hash, created_at) VALUES (` + q(a.adminUser) + `, ` + q(passHash(a.adminPass)) + `, ` + q(time.Now().Format(time.RFC3339)) + `)`
	return a.sqliteExec(sql)
}

func (a *App) handleGenerate(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodOptions {
		writeCORS(w)
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if r.Method != http.MethodGet && r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, jsonMap{"ok": false, "error": "method not allowed"})
		return
	}
	start := time.Now()
	input := strings.TrimSpace(r.URL.Query().Get("input"))
	source := "api_get"
	if r.Method == http.MethodPost {
		source = "api_post"
		var req struct {
			Input string `json:"input"`
		}
		_ = json.NewDecoder(r.Body).Decode(&req)
		input = strings.TrimSpace(req.Input)
	}
	if input == "" {
		writeJSON(w, http.StatusBadRequest, jsonMap{"ok": false, "error": "input 不能为空"})
		return
	}
	result, err := a.generateFromInput(input)
	duration := time.Since(start).Milliseconds()
	if err != nil {
		_ = a.insertHistory(input, false, "", err.Error(), source, duration)
		writeJSON(w, http.StatusUnprocessableEntity, jsonMap{"ok": false, "error": err.Error()})
		return
	}
	_ = a.insertHistory(input, true, result, "", source, duration)
	writeJSON(w, http.StatusOK, jsonMap{"ok": true, "input": input, "result": result, "duration_ms": duration})
}

func (a *App) generateFromInput(input string) (string, error) {
	key, ok := normalizeKey(input)
	if !ok {
		return "", errors.New("解析失败，请输入 IMDb ID 或豆瓣链接")
	}
	if v, hit := a.cacheGet(key); hit {
		return v, nil
	}
	result := formatResult(input)
	_ = a.cacheSet(key, result)
	return result, nil
}

func normalizeKey(input string) (string, bool) {
	imdbRe := regexp.MustCompile(`(?i)(tt\d{6,10})`)
	if m := imdbRe.FindStringSubmatch(input); len(m) > 1 {
		return "imdb:" + strings.ToLower(m[1]), true
	}
	doubanRe := regexp.MustCompile(`(?i)movie\.douban\.com/subject/(\d+)`)
	if m := doubanRe.FindStringSubmatch(input); len(m) > 1 {
		return "douban:" + m[1], true
	}
	return "", false
}

func formatResult(input string) string {
	return fmt.Sprintf("[img]https://placehold.co/600x900?text=ptgen-go[/img]\n\n◎输入        %s\n◎说明        由 Go 服务处理（可继续接入真实抓取逻辑）", input)
}

func (a *App) cacheGet(key string) (string, bool) {
	rows, err := a.sqliteJSON(`SELECT value, updated_at FROM cache WHERE key = ` + q(key) + ` LIMIT 1`)
	if err != nil || len(rows) == 0 {
		return "", false
	}
	updated, _ := toInt64(rows[0]["updated_at"])
	if time.Since(time.Unix(updated, 0)) > a.cacheTTL {
		_ = a.sqliteExec(`DELETE FROM cache WHERE key = ` + q(key))
		return "", false
	}
	if v, ok := rows[0]["value"].(string); ok {
		return v, true
	}
	return "", false
}

func (a *App) cacheSet(key, value string) error {
	now := strconv.FormatInt(time.Now().Unix(), 10)
	sql := `INSERT INTO cache (key, value, updated_at) VALUES (` + q(key) + `, ` + q(value) + `, ` + now + `)
ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at`
	return a.sqliteExec(sql)
}

func (a *App) insertHistory(input string, ok bool, result, errMsg, source string, durationMs int64) error {
	okInt := "0"
	if ok {
		okInt = "1"
	}
	sql := `INSERT INTO history (input, ok, result, error, source, duration_ms, created_at) VALUES (` +
		q(input) + `, ` + okInt + `, ` + q(result) + `, ` + q(errMsg) + `, ` + q(source) + `, ` + strconv.FormatInt(durationMs, 10) + `, ` + q(time.Now().Format(time.RFC3339)) + `)`
	return a.sqliteExec(sql)
}

func (a *App) handleAdminLogin(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodOptions {
		writeCORS(w)
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, jsonMap{"ok": false, "error": "method not allowed"})
		return
	}
	var req struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, jsonMap{"ok": false, "error": "invalid json"})
		return
	}
	rows, err := a.sqliteJSON(`SELECT password_hash FROM users WHERE username = ` + q(strings.TrimSpace(req.Username)) + ` LIMIT 1`)
	if err != nil || len(rows) == 0 {
		writeJSON(w, http.StatusUnauthorized, jsonMap{"ok": false, "error": "用户名或密码错误"})
		return
	}
	stored, _ := rows[0]["password_hash"].(string)
	if stored == "" || stored != passHash(req.Password) {
		writeJSON(w, http.StatusUnauthorized, jsonMap{"ok": false, "error": "用户名或密码错误"})
		return
	}
	token, err := a.makeJWT(strings.TrimSpace(req.Username))
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, jsonMap{"ok": false, "error": "token生成失败"})
		return
	}
	writeJSON(w, http.StatusOK, jsonMap{"ok": true, "token": token})
}

func (a *App) handleAdminStats(w http.ResponseWriter, r *http.Request, _ string) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, jsonMap{"ok": false, "error": "method not allowed"})
		return
	}
	hRows, _ := a.sqliteJSON(`SELECT COUNT(1) AS cnt FROM history`)
	cRows, _ := a.sqliteJSON(`SELECT COUNT(1) AS cnt FROM cache`)
	hCnt, _ := toInt(hRows[0]["cnt"])
	cCnt, _ := toInt(cRows[0]["cnt"])
	writeJSON(w, http.StatusOK, jsonMap{
		"ok": true,
		"stats": jsonMap{
			"history_count": hCnt,
			"cache_count":   cCnt,
			"cache_ttl_sec": int(a.cacheTTL.Seconds()),
		},
	})
}

func (a *App) handleAdminHistory(w http.ResponseWriter, r *http.Request, _ string) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, jsonMap{"ok": false, "error": "method not allowed"})
		return
	}
	limit := 50
	if s := r.URL.Query().Get("limit"); s != "" {
		if v, err := strconv.Atoi(s); err == nil && v > 0 && v <= 500 {
			limit = v
		}
	}
	rows, err := a.sqliteJSON(fmt.Sprintf(`SELECT id, input, ok, error, source, duration_ms, created_at FROM history ORDER BY id DESC LIMIT %d`, limit))
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, jsonMap{"ok": false, "error": "query failed"})
		return
	}
	writeJSON(w, http.StatusOK, jsonMap{"ok": true, "items": rows})
}

func (a *App) handleAdminCacheClear(w http.ResponseWriter, r *http.Request, _ string) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, jsonMap{"ok": false, "error": "method not allowed"})
		return
	}
	before, _ := a.sqliteJSON(`SELECT COUNT(1) AS cnt FROM cache`)
	cnt, _ := toInt(before[0]["cnt"])
	if err := a.sqliteExec(`DELETE FROM cache`); err != nil {
		writeJSON(w, http.StatusInternalServerError, jsonMap{"ok": false, "error": "clear cache failed"})
		return
	}
	writeJSON(w, http.StatusOK, jsonMap{"ok": true, "deleted": cnt})
}

func (a *App) makeJWT(username string) (string, error) {
	head, _ := json.Marshal(jsonMap{"alg": "HS256", "typ": "JWT"})
	pay, _ := json.Marshal(jsonMap{
		"sub": username,
		"iss": a.issuerName,
		"exp": time.Now().Add(24 * time.Hour).Unix(),
		"iat": time.Now().Unix(),
	})
	h := base64.RawURLEncoding.EncodeToString(head)
	p := base64.RawURLEncoding.EncodeToString(pay)
	sig := signJWTPart(h+"."+p, a.jwtSecret)
	return h + "." + p + "." + sig, nil
}

func (a *App) parseJWT(token string) (string, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return "", errors.New("invalid token")
	}
	if signJWTPart(parts[0]+"."+parts[1], a.jwtSecret) != parts[2] {
		return "", errors.New("invalid signature")
	}
	payloadBytes, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return "", err
	}
	var payload map[string]any
	if err := json.Unmarshal(payloadBytes, &payload); err != nil {
		return "", err
	}
	exp, _ := toInt64(payload["exp"])
	if exp == 0 || time.Now().Unix() > exp {
		return "", errors.New("token expired")
	}
	sub, _ := payload["sub"].(string)
	if sub == "" {
		return "", errors.New("invalid sub")
	}
	return sub, nil
}

func signJWTPart(msg string, key []byte) string {
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(msg))
	return base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
}

type adminHandler func(w http.ResponseWriter, r *http.Request, username string)

func (a *App) auth(next adminHandler) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodOptions {
			writeCORS(w)
			w.WriteHeader(http.StatusNoContent)
			return
		}
		authz := strings.TrimSpace(r.Header.Get("Authorization"))
		if !strings.HasPrefix(authz, "Bearer ") {
			writeJSON(w, http.StatusUnauthorized, jsonMap{"ok": false, "error": "missing bearer token"})
			return
		}
		user, err := a.parseJWT(strings.TrimSpace(strings.TrimPrefix(authz, "Bearer ")))
		if err != nil {
			writeJSON(w, http.StatusUnauthorized, jsonMap{"ok": false, "error": "invalid token"})
			return
		}
		next(w, r, user)
	}
}

func writeCORS(w http.ResponseWriter) {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
	w.Header().Set("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	writeCORS(w)
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		log.Printf("%s %s %s", r.Method, r.URL.Path, time.Since(start).String())
	})
}

func envOr(k, def string) string {
	v := strings.TrimSpace(os.Getenv(k))
	if v == "" {
		return def
	}
	return v
}

func envInt(k string, def int) int {
	v := strings.TrimSpace(os.Getenv(k))
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return n
}

func toInt(v any) (int, error) {
	i64, err := toInt64(v)
	return int(i64), err
}

func toInt64(v any) (int64, error) {
	switch x := v.(type) {
	case float64:
		return int64(x), nil
	case string:
		return strconv.ParseInt(x, 10, 64)
	case int64:
		return x, nil
	case int:
		return int64(x), nil
	default:
		return 0, fmt.Errorf("unsupported int type %T", v)
	}
}
