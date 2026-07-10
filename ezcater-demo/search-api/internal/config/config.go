// Package config loads service configuration from the environment with sane
// local-dev defaults. Deliberately stdlib-only: four variables don't justify a
// dependency; swap for a struct-tag env library when the count grows.
package config

import "log/slog"

type Config struct {
	Port     string // SEARCH_API_PORT
	VespaURL string // VESPA_URL
	LogLevel slog.Level
}

func Load(getenv func(string) string) Config {
	cfg := Config{
		Port:     "8090",
		VespaURL: "http://localhost:8080",
		LogLevel: slog.LevelInfo,
	}
	if v := getenv("SEARCH_API_PORT"); v != "" {
		cfg.Port = v
	}
	if v := getenv("VESPA_URL"); v != "" {
		cfg.VespaURL = v
	}
	if getenv("DEBUG") != "" {
		cfg.LogLevel = slog.LevelDebug
	}
	return cfg
}
