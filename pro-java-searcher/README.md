# Pro extension: a custom Java Searcher

This is the **real Vespa extension model** — a Java component (an OSGi bundle) that runs
inside the query path. It's optional (Lab 7 in [../docs/05-advanced-labs.md](../docs/05-advanced-labs.md)),
but doing it once is what turns "I use Vespa" into "I can make Vespa do anything."

The component: [`DefaultRankingSearcher`](src/main/java/ai/factored/vespa/DefaultRankingSearcher.java)
— if a query arrives without an explicit rank profile, it forces the hybrid `fusion` profile,
and it tags each response with a meta hit so you can see it ran. It demonstrates the three
moves every Searcher makes: **read/modify the Query → call `execution.search()` → read/modify the Result.**

```
pro-java-searcher/
├── pom.xml                                  # Maven build (packaging: container-plugin)
└── src/main/
    ├── java/ai/factored/vespa/DefaultRankingSearcher.java
    └── application/                         # the application package assembled at build time
        ├── services.xml                     # wires the searcher into the default chain
        └── schemas/doc.sd
```

## Prerequisites
- **JDK 17+** and **Maven 3.6.3+**  (`java -version`, `mvn -version`)
- Docker + the `vespa` CLI (as in [../native-app/README.md](../native-app/README.md))
- Bump `<vespa.version>` in `pom.xml` to a current Vespa 8 release if the build can't resolve `com.yahoo.vespa:container`.

## Build, deploy, run

```bash
cd pro-java-searcher
mvn clean package                 # compiles the bundle + assembles target/application/

# fresh container (if needed):  docker rm -f vespa
docker run --detach --name vespa -p 8080:8080 -p 19071:19071 vespaengine/vespa

vespa config set target local
vespa deploy --wait 300 target/application     # deploy the assembled package (incl. your bundle)

# feed the same 10 docs as the native app:
vespa feed ../native-app/sample-docs.jsonl

# Query WITHOUT specifying ranking= — the searcher forces 'fusion' for you:
vespa query \
  'yql=select id,title from sources * where userQuery() or ({targetHits:50}nearestNeighbor(embedding,q))' \
  'query=how does diet affect breathing problems' \
  'input.query(q)=embed(e5, "how does diet affect breathing problems")' \
  hits=5
```

In the response you'll see a `meta:searcher` hit with `appliedRankingProfile: fusion` — proof
your Java code ran inside Vespa and changed the request. Add `tracelevel=2` to the query to see
the searcher's trace line.

## What to take away
- A **bundle** = your Maven `artifactId`; `services.xml` references it via `bundle="pro-java-searcher"`.
- A **Searcher** is ordered into a chain with `@Before`/`@After`/`@Provides`; it can rewrite queries,
  blend/re-rank results, call external services, or short-circuit.
- The same model gives you **DocumentProcessors** (transform docs on the write path),
  **RequestHandlers** (custom HTTP endpoints), **Renderers** (custom output formats), and
  injectable **custom config** (`.def` files). See [../docs/04-pro-deep-dive.md §8](../docs/04-pro-deep-dive.md#8-extending-vespa-with-java-components).

> Note: building this pulls Vespa artifacts from Maven Central on first run. If you're time-boxed
> for the TTO presentation, this lab is the one to skip — the Python capstone + native-app already
> prove fluency; the Java component is the "I can extend the engine itself" capstone for later.
