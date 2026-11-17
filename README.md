# cc-manifest-lab

CC_Codex 二期（26.11）**数据工程**产品线：**湖区落地清单台**（Litestar + Preact CDN + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**湖区落地批次 → 对象指纹登记 → 清单闭合 → 可见性对账**

与现有七线正交（不是采集目录、ETL 管道、同步源、解析入库、编排流、种子摄取、留存裁撤）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数仓与数据湖（落地清单 / 对象登记闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、Litestar、Uvicorn、SQLite |
| 前端 | 原生 JS SPA（全中文，侧栏），靛青竖纹 + 亮青强调；不依赖外网 CDN |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 湖区落地批次列表（收集中 / 已闭合）
2. 向批次登记对象键与内容指纹
3. 闭合清单（对象数固化，拒绝空批）
4. 可见性对账报告（闭合批 vs 开放批 vs 孤儿对象）
5. 闭合清单导出（不可变清单包，导出队列留痕）
6. 下游拉包解析落库（本机 `downstream_*` 表，指纹可核）

## 清单导出（凭证定边）

两种凭证择一，本台**只启用「物化不可变清单包」**（`manifest_package`，JSON 包 + sha256），
不启用独立快照表（请求 `snapshot_table` 直接 400）。选边后果：下游读到的是包生成瞬间
**冻结的指纹集合**，之后库内再被拧动也不会漂移。

- **导出队列**：`export_jobs` 表，状态 `已生成 / 已拦截 / 失败`，队列展示包名、包 sha256、指纹集 sha256、拦截原因。
- **批次详情**：已闭合批可点「导出清单」，成功后页面出现导出芯片（包名）。
- **指纹附页**：`GET /api/exports/{id}/appendix` 读包回算 sha，附页与包对不齐即拦。
- **一致性拦截**：导出前比对库内闭合对象与批次固化数；对不齐写「已拦截」留痕并吐短句，不出产物。
- **并发防双写**：进程内批次锁 + SQLite `BEGIN IMMEDIATE` + 部分唯一索引（每批每凭证仅一条成功记录）+ 包文件 `O_EXCL` 原子创建。
- **下游落库**：`POST /api/exports/{id}/land` 验包 sha → 解析 → 与库内闭合对象逐指纹核对 → 写 `downstream_landings/downstream_objects`；包被篡改或库内指纹漂移一律拦截，重复落库 409。

预留切口（未做，供后续功能题）：清单换版生效窗、闭合作废回开放、批量失败闭互斥、跨区依赖冻结、清单回灌、快照表凭证等。

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-manifest-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:6900/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **6900**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
