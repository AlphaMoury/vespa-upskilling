// Package server wires the HTTP surface: NewServer(deps) returns an http.Handler
// with all routes attached (stdlib ServeMux, Go 1.22+ pattern routing — a query
// layer with a handful of routes needs no router dependency).
package server

import (
	"encoding/json"
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
