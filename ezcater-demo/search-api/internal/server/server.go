// Package server wires the HTTP surface: NewServer(deps) returns an http.Handler
// with all routes attached (stdlib ServeMux, Go 1.22+ pattern routing — a query
// layer with a handful of routes needs no router dependency).
package server

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"github.com/AlphaMoury/vespa-upskilling/ezcater-demo/search-api/internal/vespa"
)

func New(log *slog.Logger, vc *vespa.Client) http.Handler {
	mux := http.NewServeMux()
	addRoutes(mux, log, vc)
	return requestLog(log, cors(mux))
}

func addRoutes(mux *http.ServeMux, log *slog.Logger, vc *vespa.Client) {
	// "/{$}" matches ONLY the root path. A bare "/" pattern would be a prefix
	// match swallowing every unknown path — the {$} anchor is the 1.22-mux
	// idiom for "exactly /".
	mux.HandleFunc("GET /{$}", handleIndex(vc))
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
	})
	mux.HandleFunc("GET /readyz", handleReady(vc))
	mux.HandleFunc("GET /v1/typeahead", handleTypeahead(log, vc))
}

func handleReady(vc *vespa.Client) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if err := vc.Ready(r.Context()); err != nil {
			writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": "vespa unreachable", "error": err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, map[string]string{"status": "ready"})
	}
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v) //nolint:errcheck // headers sent; nothing to recover
}

// requestLog is the one middleware the MVP needs; otelhttp replaces it later.
func requestLog(log *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		log.Debug("http", "method", r.Method, "path", r.URL.Path, "ms", time.Since(start).Milliseconds())
	})
}

// handleIndex is a human-facing status page so localhost:8090 in a browser
// answers "is it ok?" at a glance. Machines use /healthz + /readyz; this is
// for eyes. No user input reaches the page, so fmt.Fprintf is safe here —
// the moment anything request-derived gets rendered, switch to html/template
// (which auto-escapes).
func handleIndex(vc *vespa.Client) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		vespaStatus, vespaColor := "connected", "#1f7a4d"
		if err := vc.Ready(r.Context()); err != nil {
			vespaStatus, vespaColor = "unreachable", "#bb342c"
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprintf(w, `<!doctype html><meta charset="utf-8"><title>search-api</title>
<body style="font-family:ui-monospace,Menlo,monospace;background:#0e1526;color:#dbe4f5;
  display:grid;place-items:center;min-height:95vh;margin:0">
<div style="border:1px solid #2a3654;border-radius:12px;padding:28px 34px;min-width:340px">
  <h1 style="margin:0 0 4px;font-size:20px">search-api <span style="color:#8aa0d6">· Go query layer</span></h1>
  <p style="margin:0 0 18px;color:#8aa0d6;font-size:13px">the service is up — you are looking at it</p>
  <p style="margin:6px 0">vespa&nbsp;&nbsp;&nbsp;<b style="color:%s">● %s</b></p>
  <p style="margin:18px 0 6px;color:#8aa0d6;font-size:13px">endpoints (all browser-viewable JSON):</p>
  <p style="margin:4px 0"><a style="color:#7fb5ff" href="/healthz">/healthz</a> — liveness</p>
  <p style="margin:4px 0"><a style="color:#7fb5ff" href="/readyz">/readyz</a> — readiness (pings Vespa)</p>
  <p style="margin:4px 0"><a style="color:#7fb5ff" href="/v1/typeahead?q=medi&amp;schema=dish">/v1/typeahead?q=medi</a> — gram lookup</p>
</div></body>`, vespaColor, vespaStatus)
	}
}

// cors mirrors the FastAPI dev posture so the existing Vite UI can call this
// service unchanged during the cutover. Tighten before any non-local deploy.
func cors(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}
