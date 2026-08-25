const ERROR_PREFIX = /^(?:(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:Error|Exception|Failure)|Error):\s*/i;
const CONTEXT_PREFIX = /^(?:回合失败|任务失败|操作失败|错误)[：:]\s*/u;

export function formatUserError(error: unknown): string {
  let text = error instanceof Error ? error.message : String(error ?? "");
  text = text.trim();
  if (!text) return "操作失败，请稍后重试";

  const lowered = text.toLowerCase();
  if (lowered.includes("cannot start a new session while a turn is running")) return "当前回合仍在执行，暂时无法新建会话";
  if (lowered.includes("current session is still starting")) return "新会话正在启动，请稍候";
  if (lowered.includes("current turn is stopping")) return "当前回合正在停止，请稍候";
  if (lowered.includes("empty task")) return "请输入任务后再提交";
  if (lowered.includes("python session is not ready")) return "会话尚未准备好，请稍候";
  if (lowered.includes("python session failed to start")) return "会话启动失败，请稍后重试";
  if (lowered.includes("api error") || lowered.includes("model error")) return "模型请求失败，请稍后重试";
  if (lowered.includes("open tui bridge") && (lowered.includes("closed") || lowered.includes("exited"))) return "连接已断开，请重新打开任务";
  if (lowered.includes("command not found")) return "找不到该命令";
  if (lowered.includes("modulenotfounderror") || lowered.includes("module not found")) return "缺少运行依赖，请检查环境";
  if (lowered.includes("no such file or directory") || lowered.includes("filenotfounderror")) return "找不到指定文件或目录";
  if (lowered.includes("not a directory") || lowered.includes("isadirectoryerror") || lowered.includes("notadirectoryerror")) return "文件路径无效";
  if (lowered.includes("permission denied") || lowered.includes("permissionerror")) return "没有权限执行该操作";
  if (lowered.includes("timed out") || lowered.includes("timeout") || lowered.includes("timeouterror")) return "操作超时，请稍后重试";
  if (lowered.includes("connection") || lowered.includes("network")) return "连接服务失败，请稍后重试";
  if (lowered.includes("invalid json") || lowered.includes("jsondecode") || lowered.includes("jsondecodeerror")) return "返回数据格式异常，请稍后重试";
  if (lowered.includes("calledprocesserror") || lowered.includes("exit code") || lowered.includes("command failed")) return "命令执行失败，请检查命令后重试";
  if (lowered.includes("assertionerror") || lowered.includes("verification failed")) return "结果校验失败，请检查后重试";
  if (lowered.includes("cancelled") || lowered.includes("canceled")) return "操作已取消";
  if (lowered.includes("interaction not found")) return "交互已失效，请重新操作";
  if (lowered.includes("unknown ") || lowered.includes("unsupported ")) return "不支持的操作，请重试";
  if (lowered.startsWith("[blocked]") || lowered.includes(" blocked")) return "操作被安全策略拦截";

  text = CONTEXT_PREFIX.test(text) ? text.replace(CONTEXT_PREFIX, "").trim() : text;
  while (ERROR_PREFIX.test(text)) text = text.replace(ERROR_PREFIX, "").trim();
  if (/[一-鿿]/u.test(text) && !/[A-Za-z]{2,}/u.test(text)) return text || "操作失败，请稍后重试";
  return "操作失败，请稍后重试";
}
