# I-1 P2 candidate byte manifest — 2026-10-22

Session #15 MDF Product Control / Owner-local。仅 I1 原 worktree。
727 项邻接回归通过（I1 55 项）；Ruff check/format 通过。
后续独立 Reviewer 必须先核对下列字节，任何不符都不能沿用本轮验证。
这份 manifest 不代表独立 Review PASS、AO seal 或 Git/部署/准入授权。

四个实现/测试文件已与回归运行时的 `/tmp/mdf-i1-p2-tested-source.json` 比对，
摘要一致。表中包含其他三个 I1 文档；manifest 本身不作递归自校验。

| 文件 | bytes | SHA-256 |
|---|---:|---|
| `src/market_data_foundation/integration/market_vertical.py` | 15083 | `c20224053edd5c2aceafa8de94fa5919b0bf500aaf279d33634eb8a46eafd3c5` |
| `src/market_data_foundation/integration/market_vertical_read.py` | 20469 | `c8b56f1c20bbfaee6d777ebe343615f62fb3fb465dd58ca0d0442b6a83f2658f` |
| `tests/test_i1_market_vertical.py` | 34090 | `dea7c6d0122061b003663228fb398604f061567d2f4e7c8ac30aaf0d0dec257e` |
| `tests/test_i1_calendar_evidence.py` | 9894 | `c2f0f636eb7e220d93523524ffd123e83c2559f7906c0cc051ade185476e522d` |
| `docs/r5-i7-i1/PORT.md` | 12185 | `c05680e530b2646e14fc9e3c281aab92c12832d276b25aadbf75aafeaf6a83f3` |
| `docs/r5-i7-i1/RETURN.md` | 8382 | `95d81234c7cf0f0d159eca1dbbd2486f5e87e5427977e4c088b25733d784a417` |
| `docs/r5-i7-i1/P2_RETURN.md` | 6987 | `dcb04061a1a15a14e384e6f54ae6bc6b700567d87a4383306040d5731c723531` |

原生产代码（RED 执行前工作树摘要，未使用 Git）：

| 文件 | SHA-256 |
|---|---|
| `src/market_data_foundation/integration/market_vertical.py` | `0246b4270d13f3a5f78cc4aa46c961cb8e59af2da0525d0f5f05fc5845d1e0fa` |
| `src/market_data_foundation/integration/market_vertical_read.py` | `bc6118f109a8e2d94c2482523d2eec552d9b7e59037f834fa9f4e7123d7eae30` |

本地运行证据（仅 /tmp；不是生产材料）：

| 文件 | SHA-256 |
|---|---|
| `/tmp/mdf-i1-p2-red-final.txt` | `05e967c5105b1c3b2787f68c3c811c3f4687b8a2f604b866703d83d1982b9044` |
| `/tmp/mdf-i1-p2-green.txt` | `7b2815eb716780667456217f96ed9f7104e9ef14a34f413f26e8193192f3fb1e` |
| `/tmp/mdf-i1-p2-regression.txt` | `dd201d9eb3e4c59c5fb40c8367e312b3c14c18f9c266a8c8ed894c6499c0b848` |
| `/tmp/mdf-i1-p2-regression.xml` | `e9cd66c8c5f39cac4bfd6ba399c681214a9929b6f7048ba19e509cfc9a96dcf4` |
| `/tmp/mdf-i1-p2-hygiene.py` | `b2bf2aa48aa0b1527f62b42742165094b6dd3b4db209f83d13228c94b36c3e92` |

RED 初始 13 项：9 failed / 4 passed；首次修正 47 passed；最终增加 8 项
身份/逐日分区检查后共 727 passed / 7 warnings。命令和边界见 `P2_RETURN.md`。
工作树 hygiene 使用原脚本规则与 rg 枚举；不访问 Git index，不执行 Git 命令。
临时测试 journal/运行缓存只在 /tmp；真实 Provider、PG/Garage、部署效果均为 0。
