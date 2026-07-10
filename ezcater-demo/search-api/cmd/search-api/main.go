// search-api is the Go query layer in front of Vespa — the service ezCater-style
// architecture puts between clients and the engine. It owns the search contract
// (flat request/response shapes) and translates it to YQL; Vespa owns matching,
// ranking, and embedding inference.
package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/AlphaMoury/vespa-upskilling/ezcater-demo/search-api/internal/config"
	"github.com/AlphaMoury/vespa-upskilling/ezcater-demo/search-api/internal/server"
	"github.com/AlphaMoury/vespa-upskilling/ezcater-demo/search-api/internal/vespa"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := run(ctx, os.Getenv); err != nil {
		fmt.Fprintln(os.Stderr, "fatal:", err)
		os.Exit(1)
	}
}

// run is the real entrypoint: main() stays a shim so tests can boot the whole
// service with their own ctx/env and poll /readyz (the Grafana/Mat Ryer pattern).
func run(ctx context.Context, getenv func(string) string) error {
	cfg := config.Load(getenv)
	log := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: cfg.LogLevel}))

	vc := vespa.New(cfg.VespaURL)
	srv := &http.Server{
		Addr:              ":" + cfg.Port,
		Handler:           server.New(log, vc),
		ReadHeaderTimeout: 5 * time.Second,
	}

	errCh := make(chan error, 1)
	go func() {
		log.Info("listening", "addr", srv.Addr, "vespa", cfg.VespaURL)
		if err := srv.ListenAndServe(); !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
		}
	}()

	select {
	case err := <-errCh:
		return err
	case <-ctx.Done():
		log.Info("shutting down")
		shutCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		return srv.Shutdown(shutCtx)
	}
}
