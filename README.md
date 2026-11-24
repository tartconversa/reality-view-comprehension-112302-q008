# 实时驾驶视图的理解度证据与功能开放

用于本地分析开发的合成车辆运营资料与配套 Python 应用。所有车辆、地区、
道路、调查参与者及事件标识均为虚构值；新闻报道中的厂商发布信息不构成
对理解效果的实测结论。

应用回答三个彼此独立的问题：**功能是否出现、用户是否点击目标、用户
能否正确解释系统意图**，并按显示版本、任务类型与可观察程度披露理解度
边界，最终给出"补做任务研究 / 限制试用人群 / 暂缓开放"的可追溯建议，
同时维护研究设备、模拟路线、参与者授权、任务与界面配置纪元。

Python 3.11 至 3.14 可创建独立环境，仅依赖标准库。

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
python -I -m reality_view_comprehension demo --workdir work/demo   # 端到端演示
python -I -m unittest discover -s tests -v                          # 全部测试
```

Windows PowerShell 可使用 `.venv\Scripts\Activate.ps1` 激活环境。
`data/` 中的 CSV 使用 UTF-8 BOM 编码，首行是字段名。

## 文档

- [`docs/资料契约.md`](docs/资料契约.md) — 输入资料字段、来源边界与业务事实约定
- [`docs/版本兼容清单.md`](docs/版本兼容清单.md) — 三套版本号的对齐组合（代码镜像在 `domain.VERSION_ALIGNMENT`）
- [`docs/分析口径.md`](docs/分析口径.md) — 可观察程度分级、指标定义、决策门槛与缺口披露
- [`docs/运行说明.md`](docs/运行说明.md) — 完整中文运行手册（导入/分析/决策/计划/合成数据）

## 常用命令

```sh
python -I -m reality_view_comprehension import  --db work/rvc.db --data-dir data
python -I -m reality_view_comprehension analyze --db work/rvc.db --out work/report
python -I -m reality_view_comprehension decide  --db work/rvc.db
python -I -m reality_view_comprehension synthetic --out work/synthetic
```

## 源码结构

```
src/reality_view_comprehension/
  domain.py        枚举、版本兼容清单、证据与指标数据结构
  ingest.py        CSV -> SQLite 幂等导入、校验隔离、孤儿/误触标记
  observability.py L1–L4 可观察程度分级
  analysis.py      三维分层分析、理解度边界与报告导出（CSV/JSON/Markdown）
  decision.py      补做/限制/暂缓建议与驾驶员试用资格
  planning.py      设备/路线/授权/任务状态机/配置纪元与版本封存
  synthetic.py     固定种子合成数据生成器（含全部边界场景）
  cli.py           统一命令行
tests/
  test_materials.py 基线资料契约测试
  test_pipeline.py  全链路应用测试
```
