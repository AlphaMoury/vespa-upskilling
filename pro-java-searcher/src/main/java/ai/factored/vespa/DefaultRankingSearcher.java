package ai.factored.vespa;

import com.yahoo.search.Query;
import com.yahoo.search.Result;
import com.yahoo.search.Searcher;
import com.yahoo.search.result.Hit;
import com.yahoo.search.searchchain.Execution;

/**
 * A minimal but real Searcher: it sits in the query chain, runs before/after the
 * downstream searchers, and demonstrates the three things every Searcher does —
 * read/modify the Query, invoke the rest of the chain, read/modify the Result.
 *
 * Behaviour:
 *   1. If the caller did not pick a rank profile (it's the implicit "default"),
 *      force the hybrid "fusion" profile — a typical "sane default" business rule.
 *   2. After searching, attach a small meta hit describing what happened, so you
 *      can see the searcher took effect.
 *
 * Searchers must be cheap and thread-safe: one instance serves many requests, so
 * keep state in locals (as here), never in mutable instance fields.
 */
public class DefaultRankingSearcher extends Searcher {

    private static final String IMPLICIT_DEFAULT = "default";
    private static final String PREFERRED_PROFILE = "fusion";

    @Override
    public Result search(Query query, Execution execution) {
        String requested = query.getRanking().getProfile();
        if (IMPLICIT_DEFAULT.equals(requested)) {
            query.getRanking().setProfile(PREFERRED_PROFILE);
            query.trace("DefaultRankingSearcher: no profile requested -> using '" + PREFERRED_PROFILE + "'", true, 2);
        }

        // Hand control to the rest of the chain (query parsing, dispatch, ranking, blending).
        Result result = execution.search(query);

        // Annotate the response so the effect is visible in the output.
        Hit meta = new Hit("meta:searcher", 0.0);
        meta.setField("appliedRankingProfile", query.getRanking().getProfile());
        meta.setField("totalHitCount", result.getTotalHitCount());
        result.hits().add(meta);

        return result;
    }
}
