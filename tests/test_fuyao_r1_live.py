"""live collection 保持离线；外部审查完成前，显式 opt-in 也失败。"""

import pytest

from provider_lab.fuyao_r1 import run_live


@pytest.mark.live_provider
def test_fuyao_r1_live_requires_proven_executor():
    # 明确 FAIL，不能以 skip/PASS 暗示已获 live qualification。
    run_live()
