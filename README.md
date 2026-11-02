# Market-Data-Foundation

Market-Data-Foundation is the market-data foundation product. Its current state is
runtime baseline qualification, a reusable Provider Qualification harness and the
[TDX R1 implementation bake-off](docs/qualification/tdx-r1.md). R1 identifies
tdx-python 0.1.6 as an implementation candidate with explicit endpoint pinning,
redial disabled and capability/source limitations.
MDF Primary Provider binding is not yet authorized; no production adapter is bound.
The [nonproduction Go/TDX watchlist MVP](docs/p5/real-go-tdx-watchlist-mvp-r1.md)
adds an explicitly launched, bounded real source with local Ingress/Read and IPC ACK.
Its live verification is scheduled separately by PM/Host.
The [local latest Watchlist Read](docs/p5/watchlist-latest-read-r1.md) exposes
fully Go-ACKed snapshots through a Python API and local CLI, including while the
producer runs. Consumers need a readable private MDF-dev session directory;
this is not a network-published MBO Runtime/API or canonical Publication.

## Development

Use uv-managed Python 3.14.8 and uv 0.12.22. Debian's system Python remains separate.
The committed `uv.lock` fixes development
dependencies; uv manages the local environment.

```sh
uv sync --locked --group dev
uv build
uv run --locked python scripts/check_repository_hygiene.py
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
```

- `src/market_data_foundation/`: Python package namespace.
- `tests/`: deterministic tests; ordinary pytest excludes `live_provider` tests.
- `provider_lab/`: qualification fixtures, observation schema and usage contract.
- `docs/architecture/`: canonical home for future stable architecture records.

CI runs on pushes to `main`, pull requests and manual dispatch. It uses read-only
repository permissions and does not persist checkout credentials. It accesses no
market-data providers or private LAN services. The external MDF development
PostgreSQL capability at `192.168.10.4` is never a CI prerequisite. QMT, ClickHouse
and Redis realtime infrastructure has not been materialized.

Local runtime configuration lives in ignored `config/local/runtime.toml` and
`config/local/secret.toml`; local TLS material belongs in ignored `tls/` only when
required. CI never reads these files. See `provider_lab/README.md` for the harness
and `docs/qualification/q0-runtime.md` for tested runtime/candidate evidence.

`MDF_CI_READ_PAT` is reserved for future read-only access to other private
repositories. Same-repository CI does not require it. Never put its value, other
credentials, private keys or production data in this repository.

## 宏观连续时序与共用 WebUI

当前实现包含 CPI、PPI、PCE、美国就业、美国零售销售、消费收入、GDP与Claims的连续查询、
显式采集、周期策略、执行记录和API说明；各族部署权限及真实验证范围须分别核对。
Claims 的六项全国周序列、FRED/DOL语义、实际DEV/浏览器证据和未验证边界见
[Claims R1](docs/claims-r1.md)。
开发与审查统一入口见 [基线指南](docs/macro-family-development-review.md)，
其中列明来源差异、代码复用、现有测试和 DEV 人工核对路径。
参见 [Web 快速上手](docs/cpi-webui-quickstart-zh.md)、
[CLI / 本机 API](docs/cpi-timeseries-cli-zh.md) 与
[发布历史设计](docs/macro-publication-history-design.md)。服务地址与权限以 DEV 部署配置为准。

从仓库根准备 Python 环境后，在前端目录执行：

```sh
uv sync --locked --group dev
cd web
npm ci
npm run check
npm test
npm run build
```

当前查询本地优先，真实缺口仅临时读取上游、不落库；strict 历史查询仅按本地 available 截止。
初始化 revision=1 不等于官方首发，官方全部 vintage 仍 unsupported；真实未来发布仍待自然验证。

PPI 的来源差异及历史 DEV 验证证据见 [PPI 连续时间序列速览](docs/ppi-timeseries-quickstart-zh.md)。
