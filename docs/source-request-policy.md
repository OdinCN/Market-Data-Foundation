# MDF External Source Request Policy

market_data_foundation.source_request_policy.SOURCE_REQUEST_POLICY is the single canonical retry policy for read-only/stateless external Source requests. Provider adapters do not define independent retry budgets or retryable HTTP status sets.

One logical Source request may use at most three transport attempts: the initial attempt plus two retries. Retry delays are 1 second and 2 seconds. Transient network/transport failures and HTTP 408, 429, 500, 502, 503, and 504 are retryable. Redirects, authentication/authorization failures, missing resources, invalid requests, credential failures, response-size/header/content failures, and schema/parser/semantic/contract failures are terminal for that logical request.

A successful complete provider response ends the retry boundary. Parsing, retention, Garage writes, PostgreSQL Version/Publication work, and local readback do not requery the Provider if they later fail. Storage/database outcome-unknown states continue to use their existing readback/reconciliation rules.

Logical requests and transport attempts are separate accounting dimensions. BLS CPI/PPI/Employment each issue one logical request with up to three transport attempts. BEA PCE issues exactly two ordered logical table requests, T20807 then T20804, and each table independently receives the same three-attempt transport budget. There is no Provider fallback, redirect following, discovery, pagination, or unbounded retry.

Release polling is not transport retry. For example, the CPI release runner may poll its bounded release slot when a successful HTTP response says the target reference is not available yet; each individual Source request still uses this shared three-attempt transport policy.
