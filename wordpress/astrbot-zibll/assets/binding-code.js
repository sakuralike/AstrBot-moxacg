(() => {
    const form = document.getElementById("az-code-form");
    if (!form) return;
    const code = document.getElementById("az-code");
    const copy = document.getElementById("az-copy");
    const timer = document.getElementById("az-code-time");
    const seconds = Number(timer.dataset.seconds);
    const deadline = performance.now() + seconds * 1000;
    if (seconds > 0) {
        const update = () => {
            const remaining = Math.max(0, Math.ceil((deadline - performance.now()) / 1000));
            timer.textContent = remaining ? `授权码剩余 ${remaining} 秒；请回原群 @机器人 发送。` : "授权码已过期，请重新生成；会话过期需回群重新发起。";
            if (!remaining) {
                code.value = "";
                copy.hidden = true;
                clearInterval(interval);
            }
        };
        const interval = setInterval(update, 1000);
        update();
    }
    copy.addEventListener("click", async () => {
        code.select();
        try {
            await navigator.clipboard.writeText(code.value);
            copy.textContent = "已复制";
        } catch {
            copy.textContent = "请手动复制已选中的授权码";
        }
    });
    if (form.dataset.auto === "1") {
        form.requestSubmit();
    }
})();
