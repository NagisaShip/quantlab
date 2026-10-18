/**
 * 文件选择器 accept：同时给出 MIME 与扩展名。
 * iOS Safari 对部分扩展名的识别不完整，Android 文件管理器则更依赖 MIME；两者都列上。
 */
export const UPLOAD_ACCEPT = [
  'image/*',
  'application/pdf',
  'text/plain',
  'text/csv',
  'application/json',
  'application/zip',
  '.txt', '.md', '.csv', '.json', '.py', '.yaml', '.yml', '.tsv', '.log', '.pdf', '.zip',
].join(',')
