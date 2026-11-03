"""Manual browser fixture: python3 -m tests.web_demo (never uses a real model).

Creates its own temporary SSHARNESS_HOME and fake providers. Prints only the home and
port; obtain a one-time login link with SSHARNESS_HOME=<printed home> bin/supersuperharness web.
Ctrl-C stops this fixture, not the installed service. Files stay for inspection.
Set SSHARNESS_DEMO_C1=1 to seed every existing task status, a Router question,
a reminder and a committed diff, all through the disposable fixture's own service.
Set SSHARNESS_DEMO_C2=1 for an item DAG, sticky satisfied dependencies, question
choices and an unassigned Worker. It takes precedence over the C1 fixture.
Set SSHARNESS_DEMO_V2_1=1 for Upcoming, a reminder confirmation and a named Worker chip.
"""
import asyncio
import os
from pathlib import Path
import subprocess
import tempfile

from super_super_harness import config
from super_super_harness.daemon import Service
from tests.support import Gateway, free_port, write_config


async def until(predicate):
    async with asyncio.timeout(20):
        while not predicate():
            await asyncio.sleep(.03)


async def seed_report_task(service, item=None, name="网页验收报告"):
    repo = service.cfg.home / "demo-repo"
    repo.mkdir()

    def git(cwd, *args):
        subprocess.run(["git", "-c", "user.name=Browser Fixture", "-c",
                        "user.email=fixture@example.invalid", *args], cwd=cwd,
                       check=True, capture_output=True)

    git(repo, "init", "-b", "fixture")
    (repo / "verification.md").write_text("# Browser verification\n\nPending.\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "test fixture: initial document\n\nCo-Authored-By: Claude Code <noreply@anthropic.com>")
    done = service.tasks.start(name, "ECHO Browser checks completed", repo=str(repo), item=item)
    await until(lambda: service.store.task(done["id"])["status"] == "completed")
    workspace = Path(service.store.task(done["id"])["workspace"]) / "repo"
    (workspace / "verification.md").write_text("# Browser verification\n\nNavigation and message delivery verified.\n")
    git(workspace, "add", ".")
    git(workspace, "commit", "-m", "test fixture: verification report\n\nCo-Authored-By: Claude Code <noreply@anthropic.com>")
    service.tasks.report_progress(done["id"], "已验证导航与消息发送，报告和 diff 可查看")
    service.tasks.report_done(done["id"], "浏览器验收完成。\n- 导航正常\n- 消息发送正常\n- 结果保存在 verification.md")
    return done


async def seed_c1_history(service):
    await seed_report_task(service)
    failed = service.tasks.start("失败恢复样例", "ALWAYSFAIL")
    await until(lambda: service.store.task(failed["id"])["status"] == "failed")
    interrupted = service.tasks.start("已中断的检查", "TOOLSLEEP 900")
    await until(lambda: service.store.task(interrupted["id"])["started_once"])
    await service.tasks.hold(interrupted["id"])
    service.tasks.release(interrupted["id"])
    stopped = service.tasks.start("已停止的探索", "TOOLSLEEP 900")
    await until(lambda: service.store.task(stopped["id"])["started_once"])
    await service.tasks.stop(stopped["id"])


async def seed_c2(service):
    code = service.items.create("ui-demo", "网页交互验收", "delivery", "act")["code"]
    root = await seed_report_task(service, item=code, name="入口检查")
    review = service.tasks.start("交互回归", "SLEEP 2", item=code, wait_for=[root["id"]])
    await until(lambda: service.store.task(review["id"])["started_once"])
    service.tasks.ask_user(review["id"], "继续验证网页交互与窄屏布局？", ["继续验证", "先补充说明"])
    service.tasks.report_progress(review["id"], "入口检查已完成，等待你的验证意见")
    await until(lambda: review["id"] not in service.tasks.live)
    free = service.tasks.start("补充意见", "SLEEP 1")
    await until(lambda: service.store.task(free["id"])["started_once"])
    service.tasks.ask_user(free["id"], "请补充验收意见（自由文本）。")
    await until(lambda: free["id"] not in service.tasks.live)
    styles = service.tasks.start("样式检查", "TOOLSLEEP 900", item=code, wait_for=[root["id"]])
    await until(lambda: service.store.task(styles["id"])["progress"].startswith("Bash"))
    service.tasks.report_progress(styles["id"], "正在检查主题、布局和输入区")
    service.tasks.start("结果汇总", "ECHO Verification summary", item=code, wait_for=[review["id"], styles["id"]])
    service.tasks.start("独立浏览器观察", "TOOLSLEEP 900")
    service.tasks.start("操作验证", "TOOLSLEEP 900")
    service.items.update(code, progress="入口已完成，交互回归等待确认", next="完成样式检查与结果汇总")
    await until(lambda: not service.store.current_turn() and
                not service.store.one("SELECT 1 FROM messages WHERE status!='done'"))
    service.router.ask_user("之前的问题：是否使用假模型验收？", ["是"])
    service.router.submit("user", "ECHO 使用假模型", "web")
    await until(lambda: not service.store.current_turn() and
                not service.store.one("SELECT 1 FROM messages WHERE status!='done'"))
    service.router.ask_user("入口检查完成。是否继续这轮验收？", ["继续验收", "暂不继续"])


async def seed_v2(service):
    code = service.items.create('web-v2', 'Web experience', 'delivery', 'act')['code']
    done = service.tasks.start('Navigation review', 'ECHO Navigation verified', item=code)
    await until(lambda: service.store.task(done['id'])['status'] == 'completed')
    service.tasks.report_done(done['id'], '**Ready for review.**\n\n## Checks\n- Navigation verified\n- Worker identities visible')
    review = service.tasks.start('Review the layout', 'SLEEP 2', profile='other', item=code, wait_for=[done['id']])
    await until(lambda: service.store.task(review['id'])['started_once'])
    service.tasks.ask_user(review['id'], 'Continue with the narrow-screen check?', ['Continue', 'Add context first'])
    service.tasks.report_progress(review['id'], 'Desktop layout verified; waiting for your choice')
    await until(lambda: review['id'] not in service.tasks.live)
    build = service.tasks.start('Polish Worker conversations', 'TOOLSLEEP 900', item=code)
    await until(lambda: service.store.task(build['id'])['progress'].startswith('Bash'))
    service.tasks.report_progress(build['id'], 'Checking the live conversation and session controls')
    service.tasks.start('Write the release notes', 'ECHO Ready', item=code, wait_for=[review['id'], build['id']])
    service.tasks.start('Independent accessibility review', 'TOOLSLEEP 900', profile='other')
    service.tasks.start('Check keyboard navigation', 'TOOLSLEEP 900')
    await until(lambda: not service.store.current_turn() and not service.store.one("SELECT 1 FROM messages WHERE status!='done'"))
    service.router.reply('The desktop layout is ready. Pixel needs your choice before the narrow-screen check; Echo is verifying the live conversation.')
    service.router.ask_user('Would you like a quick review before we continue?', ['Review first', 'Keep going'])


async def seed_v21(service):
    from datetime import datetime, timedelta

    code = service.items.create('web-v21', 'Web experience', 'delivery', 'act')['code']
    done = service.tasks.start('Navigation review', 'ECHO Navigation verified', item=code)
    await until(lambda: service.store.task(done['id'])['status'] == 'completed')
    await until(lambda: not service.store.current_turn() and not service.store.one("SELECT 1 FROM messages WHERE status!='done'"))
    turn = service.store.open_turn()
    build = await service.router_tool('task_start', {'name': 'System pages', 'brief': 'TOOLSLEEP 900', 'item': code})
    await until(lambda: service.store.task(build['task_id'])['progress'].startswith('Bash'))
    service.tasks.report_progress(build['task_id'], 'Verifying reminder cards and named Worker links')
    now = datetime.now().astimezone()
    await service.router_tool('reminder_add', {'name': 'Review the release', 'text': 'Check the screenshots and release notes.',
                                             'kind': 'notify', 'at': (now + timedelta(hours=1)).isoformat()})
    for hours, name, text, kind in [(3, 'Review accessibility', 'Check keyboard navigation.', 'run'),
                                   (2, 'Check test results', 'Review the final test report.', 'notify'),
                                   (4, 'Prepare release notes', 'Summarize the verified changes.', 'run'),
                                   (5, 'Review the mobile layout', 'Check the narrow-screen conversation.', 'notify'),
                                   (6, 'Follow up on feedback', 'Review comments from the team.', 'notify'),
                                   (7, 'Final release check', 'Confirm the release is ready.', 'run')]:
        service.reminders.add(name, text, kind, at=(now + timedelta(hours=hours)).isoformat())
    service.router.reply('Pixel is verifying the system pages. I added a reminder to review the release in an hour; your upcoming reminders and scheduled work are on the right.')
    service.store.run("UPDATE turns SET status='done', ended=? WHERE id=?", now.timestamp(), turn)


async def seed_routing(service):
    """Six active Workers, cross-item dependency and a real confirmation."""
    web = service.items.create('routing-web', 'Web experience', 'delivery', 'act')['code']
    research = service.items.create('routing-research', 'Next-version research', 'delivery', 'act')['code']
    upstream = service.tasks.start('Review the interaction', 'SLEEP 2', profile='other', item=web)
    await until(lambda: service.store.task(upstream['id'])['started_once'])
    service.tasks.ask_user(upstream['id'], 'Release the upstream result to the documentation Worker?', ['Continue', 'Add context first'])
    await until(lambda: upstream['id'] not in service.tasks.live)
    service.tasks.start('Write the release notes', 'TOOLSLEEP 900', item=research, wait_for=[upstream['id']])
    for name, item, progress in [('Polish the Routing graph', web, 'Connecting Router to each Worker'),
                                 ('Inspect tool previews', web, 'Checking command arguments and result previews'),
                                 ('Review external events', research, 'Comparing event delivery guarantees'),
                                 ('Verify keyboard access', None, 'Checking focus and narrow-screen navigation')]:
        task = service.tasks.start(name, 'TOOLSLEEP 900', item=item)
        await until(lambda: service.store.task(task['id'])['progress'].startswith('Bash'))
        service.tasks.report_progress(task['id'], progress)
        # Session-shaped fixture rows exercise Inspect without a real model or search.
        import json
        session = service.store.task(task['id'])['session_id']
        log = service.cfg.home / 'sessions' / 'proj' / (session + '.jsonl')
        records = [
            {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': 'demo-search', 'name': 'WebSearch', 'input': {'query': 'accessible routing graphs <example>'}},
                {'type': 'tool_use', 'id': 'demo-read', 'name': 'Read', 'input': {'file_path': '/demo/web/index.html'}}]}},
            {'type': 'user', 'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'demo-search', 'content': 'First result line <safe>\nSecond result line\nMore details remain expandable.'},
                {'type': 'tool_result', 'tool_use_id': 'demo-read', 'is_error': True, 'content': 'Fixture error <safe>: demonstrate an unbroken ERROR tag.'}]}}
        ]
        with log.open('a') as out:
            for record in records:
                out.write(json.dumps(record) + '\n')
    await until(lambda: not service.store.current_turn() and not service.store.one("SELECT 1 FROM messages WHERE status!='done'"))
    service.router.reply('Six Workers are coordinating the release. Nova needs your choice; Pixel is waiting for the upstream result.')


async def main():
    home = Path(tempfile.mkdtemp(prefix="ssharness-web-browser-")).resolve()
    primary, backup = Gateway(), Gateway()
    write_config(home, primary.port, backup.port, failover="")
    if any(os.environ.get(key) == '1' for key in ('SSHARNESS_DEMO_V2', 'SSHARNESS_DEMO_V2_1', 'SSHARNESS_DEMO_ROUTING')):
        profiles = home / 'profiles.toml'
        profiles.write_text(profiles.read_text().replace('work-model', 'gpt-6-astra').replace('other-model', 'claude-opus-5').replace('main-model', 'Router · fake model'))
    if os.environ.get('SSHARNESS_DEMO_ROUTING') == '1':
        profiles = home / 'profiles.toml'
        profiles.write_text(profiles.read_text().replace('max_workers = 2', 'max_workers = 8').replace('Router · fake model', 'claude/claude-opus-5-5[1m]'))
    cfg = config.load(home)
    port = free_port()
    # Persist web settings so the CLI resolves the same local fixture.
    path = home / "profiles.toml"
    path.write_text(path.read_text().replace("port = 0", 'port = %d\nuser_name = "Browser Test"' % port))
    cfg = config.load(home)
    cfg.failover.update(silent_seconds=3600, deep_probe_seconds=3600)
    service = Service(cfg)
    running = asyncio.create_task(service.run())
    try:
        while not service.router.ready:
            await asyncio.sleep(.03)
        if os.environ.get('SSHARNESS_DEMO_ROUTING') == '1':
            await seed_routing(service)
        elif os.environ.get('SSHARNESS_DEMO_V2_1') == '1':
            await seed_v21(service)
        elif os.environ.get('SSHARNESS_DEMO_V2') == '1':
            await seed_v2(service)
        elif os.environ.get("SSHARNESS_DEMO_C2") == "1":
            await seed_c2(service)
        else:
            if os.environ.get("SSHARNESS_DEMO_C1") == "1":
                await seed_c1_history(service)
            task = service.tasks.start("验证网页入口", "TOOLSLEEP 900", reason="Browser acceptance fixture")
            waiting = service.tasks.start("需要确认的测试任务", "SLEEP 2")
            while not service.store.task(waiting["id"])["started_once"]:
                await asyncio.sleep(.03)
            service.tasks.ask_user(waiting["id"], "继续用假 Harness 验证网页交互吗？")
            service.tasks.report_progress(task["id"], "等待浏览器发送消息、查看全过程并打断")
            if os.environ.get("SSHARNESS_DEMO_C1") == "1":
                from datetime import datetime, timedelta
                await until(lambda: waiting["id"] not in service.tasks.live)
                service.tasks.start("后台校验", "TOOLSLEEP 900")
                service.tasks.start("排队中的检查", "TOOLSLEEP 900")
                service.reminders.add("复查浏览器结果", "检查本轮假模型验收结果", "notify",
                                      at=(datetime.now().astimezone() + timedelta(hours=1)).isoformat())
                await until(lambda: not service.store.current_turn() and
                            not service.store.one("SELECT 1 FROM messages WHERE status!='done'"))
                service.tasks.report_progress(waiting["id"], "基础入口已检查，等待你的继续意见")
                service.router.ask_user("历史问题：是否先检查网页入口？")
                for index in range(110):
                    service.router.reply("历史进度记录 %d · 假模型浏览器夹具" % (index + 1), "notice")
                service.router.reply("已有一项任务等待你的确认，网页入口验证仍在进行。完成的报告可以在 Tasks 查看。")
                service.router.ask_user("这一轮是否继续检查窄屏？请在对话中告诉我你的偏好。")
        print("SSHARNESS_HOME=" + str(home), flush=True)
        print("WEB_PORT=" + str(port), flush=True)
        await running
    finally:
        service.stopping.set()
        if not running.done():
            await running
        primary.down()
        backup.down()


if __name__ == "__main__":
    os.environ["SSHARNESS_NO_CAFFEINATE"] = "1"
    asyncio.run(main())
