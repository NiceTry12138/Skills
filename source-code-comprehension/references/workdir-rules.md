# 工作目录规则

## 1. 生成时间戳

使用当前时间戳 `YYYYMMDD_HHMMSS` 作为本次阅读的唯一标识，以下简写为 `{ts}`。

## 2. 定位项目根目录（按优先级，命中即停止）

从当前工作路径开始**逐级向上查找**：

1. 包含版本控制标识的目录：`.git/`、`.svn/`、`.hg/`
2. 包含项目清单文件的目录：
   - `package.json`（Node/JS）
   - `pyproject.toml` / `setup.py` / `requirements.txt`（Python）
   - `Cargo.toml`（Rust）
   - `go.mod`（Go）
   - `pom.xml` / `build.gradle`（Java）
   - `*.uproject` / `*.sln`（UE / C++）
   - `CMakeLists.txt`（顶层 CMake）
3. 用户当前对话工作目录的最顶层

## 3. 工作目录路径规则

工作目录**必须**位于项目根目录正下方：

```
✅ <项目根>/ThroughtWork/throught_{ts}/
✅ <项目根>/ThroughtWork/throught_{ts}/Image/

❌ <项目根>/SomeModule/ThroughtWork/...
❌ <项目根>/src/xxx/ThroughtWork/...
❌ ~/ThroughtWork/...
```

## 4. 创建前校验流程

按顺序执行，任何异常都必须停下来向用户确认：

1. **确认项目根**：输出绝对路径给用户
2. **全局搜索**：`find . -type d -name "ThroughtWork"`（或等效命令）
3. **按结果处理**：

| 情况 | 处理 |
|------|------|
| 无任何 ThroughtWork 目录 | 直接在 `<项目根>/ThroughtWork/` 下创建 |
| 仅 `<项目根>/ThroughtWork/` 存在 | 在其下创建 `throught_{ts}/` 子目录 |
| 仅有非根目录下的 ThroughtWork | **必须在根目录下新建**，不得复用 |
| 根目录和子目录都有 ThroughtWork | 使用根目录那个，并提示用户子目录也有同名目录 |
| 根目录下已有 `throught_{ts}/` | 极少见（同秒触发）。在 `{ts}` 末尾追加 `_2`、`_3` 后缀新建，不动已有目录 |

4. **告知用户最终路径**（开始写笔记之前）：

```
项目根目录：<绝对路径>
工作目录：  <绝对路径>/ThroughtWork/throught_{ts}/
笔记文件：  <绝对路径>/ThroughtWork/throught_{ts}/through_{ts}.md
```
