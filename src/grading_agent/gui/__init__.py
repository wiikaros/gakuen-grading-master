"""GUI 包：Tkinter 双页签（跑批监控台 + 结果复核）。

入口：scripts/gui.py → grading_agent.gui.app.main()

线程约束（重要）：Playwright 同步 API 绑死创建它的线程，因此
- 监控台的 Orchestrator.run() 整个跑在 runner.py 的 worker 线程内
- 复核页签的单行重评跑在 regrade_worker.py 的独立常驻线程内
- 两者由 app.py 的 busy 标志互斥，避免并发写 xlsx / 两个浏览器实例打架
"""
