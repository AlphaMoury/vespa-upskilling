package server

import (
	"log/slog"
	"net/http"
	"net/url"
	"regexp"
	"strings"

	"github.com/AlphaMoury/vespa-upskilling/ezcater-demo/search-api/internal/vespa"
)

// titleField maps each schema to the field shown as a suggestion — the same
// field its gram index is derived from.
var titleField = map[string]string{"dish": "name", "covid": "title", "question": "text"}

var sanitize = regexp.MustCompile(`[^a-z0-9 ]`)

type suggestion struct {
	Name string `json:"name"`
}

// handleTypeahead is the gram-index lookup: 2+ sanitized chars, substring match
// on the `grams` field, unranked, deduped, top 6. Parity with the FastAPI
// endpoint so the UI can switch backends without change.
func handleTypeahead(log *slog.Logger, vc *vespa.Client) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		schema := r.URL.Query().Get("schema")
		if schema == "" {
			schema = "dish"
		}
		title, ok := titleField[schema]
		if !ok {
			writeJSON(w, http.StatusOK, map[string][]suggestion{"suggestions": {}})
			return
		}
		term := strings.TrimSpace(sanitize.ReplaceAllString(strings.ToLower(strings.TrimSpace(r.URL.Query().Get("q"))), " "))
		if len(term) < 2 {
			writeJSON(w, http.StatusOK, map[string][]suggestion{"suggestions": {}})
			return
		}

		// Fixed YQL shape; the user's text travels ONLY as the @term parameter.
		params := url.Values{
			"yql":     {"select " + title + " from " + schema + " where grams contains @term limit 40"},
			"term":    {term},
			"ranking": {"unranked"},
			"timeout": {"5s"},
		}
		resp, err := vc.Search(r.Context(), params)
		if err != nil {
			log.Warn("typeahead vespa error", "err", err)
			writeJSON(w, http.StatusOK, map[string][]suggestion{"suggestions": {}})
			return
		}

		const maxSuggestions = 6
		seen := make(map[string]bool, maxSuggestions)
		out := make([]suggestion, 0, maxSuggestions)
		for _, h := range resp.Root.Children {
			name, _ := h.Fields[title].(string)
			if name == "" || seen[strings.ToLower(name)] {
				continue
			}
			seen[strings.ToLower(name)] = true
			if len(name) > 90 {
				name = name[:90]
			}
			out = append(out, suggestion{Name: name})
			if len(out) == maxSuggestions {
				break
			}
		}
		writeJSON(w, http.StatusOK, map[string][]suggestion{"suggestions": out})
	}
}
