// Package vespa is a thin typed client over Vespa's HTTP Search API.
//
// There is no official Go library (vespa-engine/vespa#30413 is parked), and the
// professional norm — e.g. Vinted's Go gateway in front of Vespa — is exactly
// this: one shared http.Client, fixed YQL shapes, and ALL user text passed via
// Vespa's @parameter substitution so values are parsed as data, never as YQL.
package vespa

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"
)

type Client struct {
	base string
	hc   *http.Client
}

func New(base string) *Client {
	return &Client{
		base: base,
		// One process-wide client: connection reuse across all requests. The
		// per-request deadline comes from ctx; this is the hard upper bound.
		hc: &http.Client{Timeout: 20 * time.Second},
	}
}

// SearchResponse mirrors the subset of Vespa's default JSON result format we consume.
type SearchResponse struct {
	Root struct {
		Fields struct {
			TotalCount int `json:"totalCount"`
		} `json:"fields"`
		Errors   []json.RawMessage `json:"errors"`
		Children []Hit             `json:"children"`
	} `json:"root"`
}

type Hit struct {
	ID        string         `json:"id"`
	Relevance float64        `json:"relevance"`
	Fields    map[string]any `json:"fields"`
}

// Search runs a query against /search/. Callers put the YQL (with @param
// placeholders) and the parameter values into params.
func (c *Client) Search(ctx context.Context, params url.Values) (*SearchResponse, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.base+"/search/?"+params.Encode(), nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.hc.Do(req)
	if err != nil {
		return nil, fmt.Errorf("vespa search: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 32<<20))
	if err != nil {
		return nil, fmt.Errorf("vespa read: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("vespa status %d: %.300s", resp.StatusCode, body)
	}
	var out SearchResponse
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, fmt.Errorf("vespa decode: %w", err)
	}
	return &out, nil
}

// Ready reports whether the Vespa container answers its health endpoint.
func (c *Client) Ready(ctx context.Context) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.base+"/state/v1/health", nil)
	if err != nil {
		return err
	}
	resp, err := c.hc.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("vespa health status %d", resp.StatusCode)
	}
	return nil
}
