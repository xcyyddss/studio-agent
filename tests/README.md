# 运行回归测试

使用 Python 3.11 或更新版本，先在虚拟环境中执行 `python -m pip install .` 安装完整运行依赖，再在可丢弃的源码副本根目录运行。测试会在源码位置创建 `.validation/`；不要在需要保持原样的目录执行。

```powershell
python -c "from pathlib import Path; Path('.validation').mkdir(exist_ok=True)"
python -B -m unittest discover -s tests -p 'test_*.py'
```

七个顶层 Python suite 使用临时数据，HTTP 测试只监听回环地址，不调用模型，不接触正式任务。覆盖输入承接、故障重建、语义整理复用、离线容量计量、父子反馈批次、并发配置及接口校验。运行环境须允许绑定本机回环端口；若外层沙箱禁止监听，HTTP setUp 会报错。缺少 `tiktoken` 会使分词测试失败，也会使运行时降级为保守字节计量，从而影响容量测试；不能将缺依赖环境的结果当成完整回归结论。

## 网页交互测试

网页交互测试需要 Node.js、Playwright 和 Chromium。依赖可装到单独测试环境；它们不是 Studio 的部署依赖：

```powershell
npm install --no-save playwright
npx playwright install chromium
node --test tests/test_ui.cjs
```

Windows 默认使用 `C:/Program Files/Google/Chrome/Application/chrome.exe`；其他系统默认使用 Playwright 安装的 Chromium。可设置 `STUDIO_TEST_BROWSER` 指向实际存在的 Chromium 可执行文件；Windows 没有上述 Chrome 时也需覆盖。例如可使用 Playwright 自己的浏览器路径：

```powershell
$env:STUDIO_TEST_BROWSER = node -e "process.stdout.write(require('playwright').chromium.executablePath())"
node --test tests/test_ui.cjs
```

测试启动独立无头浏览器，接口响应使用合成数据；覆盖轮询时的输入焦点、草稿、目标切换、文件交互和并发配置。它不启动真实 Studio 后端，不等于完整后端端到端验收。默认测试当前源码；`STUDIO_UI_REV` 仅用于从已有 Git 修订读取网页资源，普通源码副本无需设置。测试暂存目录是 `.validation/`，不打入软件包。

这些测试验证确定性的工作流与界面行为，不证明摘要语义绝对无损、模型认知效果或服务端最大上下文容量。

## 真实模型与规模验收

`real_model_*.py` 会通过当前已登录的 Codex CLI 调用真实模型服务，可能消耗额度；需要固定模型策略对应的账户权限和兼容 CLI。它们不属于默认离线回归，使用合成目标及独立验收目录，不应替换成正式任务材料。长期运行验收可分别执行：

```powershell
python -B tests/acceptance/real_model_closure.py
python -B tests/acceptance/real_model_materials.py
python -B tests/acceptance/real_model_user_recovery.py
python -B tests/acceptance/real_model_bound_locator.py
python -B tests/acceptance/storage_scale.py
```

前四项使用合成目标/资料和真实 Astra xhigh 调用。分别覆盖内部原件交付与连续阶段等待、超议员窗口材料的完整准备及实际承接、真实队列与工作进程下 USER 故障隔离后的两次恢复、历史文件精确绑定至专项核对后被父作者承接并交付。业务循环没有轮数截止，以真实等待或交付结束；等待链还检查重启后的空推进不会创建调用。故障注入和模拟用户输入明确标为 fixture，不作为真实用户效果证据。最后一项不调用模型，生成 1,002 个 job 与约 1.64 亿字符归档，检查状态查询和空推进不加载归档正文，并记录本机耗时；它会写入大量数据，规模数值不代表任何机器上的性能结论。全部结果仅保存至 `.validation/`。

`test_wait_lifecycle.py` 连续执行等待、新输入、承接、重新打开和重复推进，覆盖旧交接不唤醒、真实到达恰好恢复、阶段交付不解除依赖、原始历史取回、无增量整理、空议会结果与有限核对全链。

真实模型的多轮等待与核对验收：

```powershell
python -B tests/acceptance/real_model_waits.py
```

使用合成配置资料与三次交接，角色调用均为全新 Astra xhigh。逐轮检查阶段回传、评估、父承接，等待后重开并空推进 40 次，断言没有新增 job；新资料到达后继续，最终经真实专项核对、结果承接和正式交付。输出在 `.validation/real-waits-*/`，不进入部署包。循环上限是测试失败保护，不是生产运行的停止规则。

`python -B tests/acceptance/real_model_generated_file.py` 另行验证作者通过 `@self` 读取同次生成的文件，下一次真实调用获得并承接正文、范围与哈希，随后重开并空推进 40 次维持等待。使用合成资料，结果保存到 `.validation/real-generated-*/`。

## Python 执行器的必要验收

涉及测试执行器的发布必须运行真实原生沙箱测试，不能只运行离线替身：

~~~powershell
python -B -m unittest discover -s tests/native -v
~~~

此套件直接调用 MXC，不调用模型。覆盖实际执行、失败输出、导入错误、零测试、全跳过、超时、提前退出、输出溢出，以及文件读写、网络和环境变量隔离。MXC 不可用会失败，不会跳过。应在允许启动 MXC 的 Windows 宿主运行，外层开发沙箱可能阻止嵌套启动；不得通过关闭工具自身的沙箱取得通过。

完整模型承接链使用独立合成任务，会真实调用已登录的 Codex CLI 模型服务：

~~~powershell
python -B tests/acceptance/real_model_sandbox.py
~~~

它依次验证真实作者生成测试、注入执行前失败、真实作者承接并等待、显式重试后的真实 MXC 执行，以及新模型调用读取结果并完成。注入失败明确标记为 fixture，不充当真实 MXC 故障证据。任何导入拒绝、测试失败、结果未承接都使验收失败。结果保存到 .validation/real-sandbox-*/acceptance.json；不会接触正式 studio-data。

## 记录结果

记录测试命令、代码版本、操作系统、Python 和有关依赖版本，以及通过、失败、错误、跳过和未执行的范围。真实调用另记 Codex CLI 版本与实际收据；原生沙箱和离线替身分开报告。已有 `.validation/` 仅是历史产物，不能当作当前机器已通过测试的证据。公开报告优先使用统计与去敏片段，不直接上传完整运行日志或目录。
