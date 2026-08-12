# Android Compose Build Pitfalls (Kanban Pipeline)

## Compose BOM 2024.02 Known API Mismatches

### 1. `rememberInfiniteTransition` 没有 `key` 参数

- Compose 1.5.x (BOM 2024.02) 不支持 `rememberInfiniteTransition(key = "...")`
- 错误：`Cannot find a parameter with this name: key`
- 修复：用 `if (isActive)` 控制动画启停，而非用 `key` 参数

```kotlin
// ❌ 错误（Compose 1.6+ only）
val transition = rememberInfiniteTransition(key = if (isActive) "breathing" else "idle")

// ✅ 正确（所有版本兼容）
if (!isActive) return@composed Modifier
val transition = rememberInfiniteTransition(label = "breathTransition")
```

### 2. `LazyVerticalGrid` `GridItemSpan` 不存在

- BOM 2024.02 不支持 `item(span = { GridItemSpan(maxLineSpan) })`
- 错误：`Unresolved reference: GridItemSpan`
- 修复：用 `item { ... }` 不加 span（取 1 列宽度），或者把全宽元素放在 grid 外面

### 3. `LifecycleStartEffect` 不存在

- BOM 2024.02 对应的 lifecycle-runtime-compose 版本没有 `LifecycleStartEffect`
- 错误：`Unresolved reference: LifecycleStartEffect`
- 修复：用 `LocalLifecycleOwner.current.lifecycle.currentStateAsState()` + `LaunchedEffect(lifecycleState)`

### 4. `WindowInsets.systemBars` 不存在

- BOM 2024.02 不支持 `WindowInsets.systemBars` 这个复合 API
- 修复：分别获取 `WindowInsets.statusBars`（顶部）+ `WindowInsets.navigationBars`（底部）

## Moshi Codegen 陷阱

### 5. `KotlinJsonAdapterFactory` 和 `moshi.codegen` 冲突

- 项目同时依赖 `moshi.codegen`(KSP) 和 `KotlinJsonAdapterFactory`(反射) → 编译错误
- 错误：`Unresolved reference: KotlinJsonAdapterFactory`
- 修复：data class 加 `@JsonClass(generateAdapter = true)`，Moshi 用 codegen 不用反射

```kotlin
@JsonClass(generateAdapter = true)
data class ProfileSnapshot(
    val inputTokens: Long = 0,
    val outputTokens: Long = 0,
    val cacheReadTokens: Long = 0,
    val sessions: Long = 0
)
```

```kotlin
// AppDatabase.kt 的 Converters：
class Converters {
    private val moshi: Moshi = Moshi.Builder().build()  // 不加任何 adapter factory
    // ...
}
```

## 编译验证

### 6. Auditor 复审通过 ≠ 代码能编译

- 本 session 发现 auditor 二审说"全部通过"但存在 3 个编译错误
- 规则：**Orchestrator 必须手动跑一次 `./gradlew assembleDebug` 验证**
- 编译失败时直接修复（不经过流水线审计），然后重新构建