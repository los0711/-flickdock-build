"""Shared native replacement shutdown for the PKG and explicit DMG installer."""
import os
import subprocess

# NSRunningApplication identifies this product by bundle ID, including its
# background accessory process. A name-only process search also matches tools
# and unrelated applications and cannot safely decide what an installer owns.
INSTALL_CLOSE_JXA=r'''ObjC.import('AppKit');
ObjC.import('Foundation');
function confirmReplacement() {
    var ui=Application.currentApplication();ui.includeStandardAdditions=true;
    ui.displayDialog('安装需要退出正在运行的 FlickDock。点击“退出并继续”后将关闭旧版本并覆盖安装，当前传输会中断。',
        {buttons:['取消安装','退出并继续'],defaultButton:'退出并继续',cancelButton:'取消安装',withTitle:'安装 FlickDock'});
}
function run(argv) {
    var excluded=Number(argv[0]),bundle='com.flickdock.app';
    if (!Number.isSafeInteger(excluded) || excluded<0) throw Error('无效的安装进程。');
    function targets() {
        var raw=$.NSRunningApplication.runningApplicationsWithBundleIdentifier(bundle),apps=[];
        for(var i=0;i<raw.count;i++) {
            var app=raw.objectAtIndex(i);
            if(app.processIdentifier!==excluded && !app.isTerminated) apps.push(app);
        }
        return apps;
    }
    function waitForExit(seconds) {
        var deadline=Date.now()+seconds*1000;
        while(targets().length && Date.now()<deadline)
            $.NSRunLoop.currentRunLoop.runUntilDate($.NSDate.dateWithTimeIntervalSinceNow(0.1));
        return targets();
    }
    var apps=targets();if(!apps.length)return 'not-running';
    confirmReplacement();
    apps.forEach(function(app){app.terminate;});
    var remaining=waitForExit(5);
    // The explicit replacement confirmation covers unresponsive background
    // instances too. Never replace files until all identified instances exit.
    remaining.forEach(function(app){app.forceTerminate;});
    if(waitForExit(3).length)throw Error('FlickDock 未能退出，安装已停止，旧版本保持不变。');
    return 'closed';
}'''


def close_for_replacement(*,runner=subprocess.run,exclude_pid=None):
    result=runner(['/usr/bin/osascript','-l','JavaScript','-e',INSTALL_CLOSE_JXA,
                   str(os.getpid() if exclude_pid is None else exclude_pid)],
                  # User confirmation may wait; shutdown itself is bounded
                  # by the native script's five- and three-second deadlines.
                  capture_output=True,text=True)
    if not result.returncode:return True
    if '(-128)' in result.stderr:return False
    raise RuntimeError('无法退出旧版 FlickDock：'+result.stderr.strip())
