/**
 * CDP 直连做「真·DOM 级」前端验证（零第三方依赖，仅用 Node 22 原生 WebSocket）。
 *
 * 用法：
 *   node dom_check.js <debugPort> <pageUrl> <outHtmlPath> [exprFile] [preExprFile]
 *
 * 做三件事：
 *   1. 连上 Chrome 的 DevTools 协议，打开页面；
 *   2. 收集 window 上的未捕获异常与控制台 error/warn —— 静默失败必须变成可见失败；
 *   3. 若给了表达式文件，就在页面里执行它（支持 await），再导出最终 DOM 到文件。
 *
 * preExprFile 用于「在被测表达式之前，往页面里塞点东西」（例如一段较大的 base64 样本）。
 * 之所以要单独一个参数而不是让调用方自己再跑一次：dom_check 每次都会 Page.navigate，
 * 跑第二次会把第一次注入的变量连同页面状态一起清掉。
 *
 * 之所以不只用 `--dump-dom`：dump-dom 只能看「首屏渲染结果」，
 * 没法验证「点一下之后会怎样」，而交互恰恰是这类工具最容易出问题的地方。
 */
const fs = require("fs");

function httpGetJson(url) {
  return new Promise((resolve, reject) => {
    const mod = url.startsWith("https") ? require("https") : require("http");
    mod
      .get(url, (res) => {
        let body = "";
        res.on("data", (chunk) => (body += chunk));
        res.on("end", () => {
          try {
            resolve(JSON.parse(body));
          } catch (error) {
            reject(new Error("无法解析 JSON：" + body.slice(0, 200)));
          }
        });
      })
      .on("error", reject);
  });
}

async function main() {
  const [debugPort, pageUrl, outHtml, exprFile, preExprFile] = process.argv.slice(2);
  if (!debugPort || !pageUrl || !outHtml) {
    console.error("用法：node dom_check.js <debugPort> <pageUrl> <outHtmlPath> [exprFile] [preExprFile]");
    process.exit(2);
  }

  const targets = await httpGetJson(`http://127.0.0.1:${debugPort}/json/list`);
  const page = targets.find((item) => item.type === "page");
  if (!page || !page.webSocketDebuggerUrl) {
    throw new Error("没有找到可用的页面调试目标");
  }

  const socket = new WebSocket(page.webSocketDebuggerUrl);
  const pending = new Map();
  let messageId = 0;
  const problems = [];

  function send(method, params) {
    const id = ++messageId;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      socket.send(JSON.stringify({ id, method, params: params || {} }));
    });
  }

  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) reject(new Error(JSON.stringify(message.error)));
      else resolve(message.result);
      return;
    }
    if (message.method === "Runtime.exceptionThrown") {
      problems.push({
        kind: "exception",
        text: message.params.exceptionDetails.text + " " +
          ((message.params.exceptionDetails.exception && message.params.exceptionDetails.exception.description) || "")
      });
    }
    if (message.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(message.params.type)) {
      problems.push({
        kind: "console." + message.params.type,
        text: (message.params.args || []).map((arg) => arg.value || arg.description || arg.type).join(" ")
      });
    }
  });

  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve);
    socket.addEventListener("error", () => reject(new Error("DevTools WebSocket 连接失败")));
  });

  await send("Runtime.enable");
  await send("Page.enable");
  await send("Page.navigate", { url: pageUrl });
  await new Promise((resolve) => setTimeout(resolve, 2500));

  let evalResult = null;
  if (exprFile && fs.existsSync(exprFile)) {
    // 先跑预注入（例如塞一段 base64 样本），再跑主表达式；
    // 两者共用同一次导航，页面状态不会丢。
    if (preExprFile && fs.existsSync(preExprFile)) {
      await send("Runtime.evaluate", {
        expression: fs.readFileSync(preExprFile, "utf8"),
        returnByValue: true,
        userGesture: true
      });
    }
    const expression = fs.readFileSync(exprFile, "utf8");
    const out = await send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true
    });
    evalResult = out && out.result ? out.result.value : null;
    await new Promise((resolve) => setTimeout(resolve, 800));
  }

  const html = await send("Runtime.evaluate", {
    expression: "document.documentElement.outerHTML",
    returnByValue: true
  });
  fs.writeFileSync(outHtml, html.result.value || "", "utf8");

  console.log(JSON.stringify({ evalResult, problems }, null, 2));
  socket.close();
}

main().catch((error) => {
  console.error("DOM 检查失败：" + error.message);
  process.exit(1);
});
