# SVG 绘制规范

## 中文不乱码（强制）

所有包含中文的 SVG 必须满足：

1. **文件编码**：以 UTF-8 保存，不使用 BOM
2. **字体声明**：在 `<style>` 中声明跨平台中文字体回退链
3. **直接写汉字**：不使用 `&#xxxx;` HTML 实体转义中文
4. **不用 `<foreignObject>`**：部分渲染器不支持

### 字体声明模板

```xml
<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600">
  <style>
    text {
      font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", "SimHei", sans-serif;
      font-size: 14px;
    }
  </style>
  <!-- 直接写中文，不转义 -->
  <text x="100" y="50">业务逻辑层</text>
</svg>
```

---

## 图表类型与内容要求

### architecture.svg — 分层架构图

**必须包含**：
- 上游调用方（谁调用本模块）
- 模块内部分层（至少 3 层）
- 下游依赖（本模块依赖什么底层服务）
- 层间用带箭头的连线，标注调用方向

**禁止**：只画目录树结构，没有上下游边界

### call-graph.svg — 调用关系图

- 节点：关键函数 / 类
- 边：带方向箭头，表示调用关系
- 可选：在边上标注调用频率（如"每帧"）

### sequence-{流程名}.svg — 时序图

**必须包含**：
- 泳道区分不同角色（不同系统 / 组件）
- 关键分支（if/else 路径）
- 循环（loop）
- 异步回调（async callback）

---

## 布局建议

- 节点最小宽度 120px，最小高度 40px，避免文字溢出
- 箭头用 `marker-end` 定义箭头头部，颜色与连线一致
- 层之间间距 ≥ 60px，避免视觉拥挤
- 关键节点用填充色区分（如：外部层用灰色，核心层用蓝色，底层依赖用橙色）

### 箭头定义模板

```xml
<defs>
  <marker id="arrow" markerWidth="10" markerHeight="7"
          refX="10" refY="3.5" orient="auto">
    <polygon points="0 0, 10 3.5, 0 7" fill="#555"/>
  </marker>
</defs>
<line x1="100" y1="50" x2="200" y2="50"
      stroke="#555" stroke-width="1.5"
      marker-end="url(#arrow)"/>
```
