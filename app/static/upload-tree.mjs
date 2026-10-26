export function captureDroppedItems(dataTransfer) {
  const items = Array.from(dataTransfer.items || []).filter(item => item.kind === 'file');
  if (!items.length) return Array.from(dataTransfer.files || [], file => ({file}));
  return items.map(item => {
    const getEntry = item.getAsEntry || item.webkitGetAsEntry;
    const entry = getEntry?.call(item);
    if (entry) return {entry};
    const file = item.getAsFile();
    if (!file) throw Error('无法读取拖入的项目，请使用上传按钮重新选择');
    return {file};
  });
}

export async function collectDroppedItems(roots, checkCancelled = () => {}, onDirectory = () => {}) {
  const files = [], directories = [];
  let count = 0;
  const validate = path => {
    if (++count > 10000) throw Error('一次最多上传 10000 个文件和文件夹，请分批上传');
    if (path.length > 2000 || path.split('/').some(part => !part || part === '.' || part === '..' || /[\\\x00-\x1f]/.test(part))) throw Error('文件路径无效');
  };
  async function visit(entry, prefix = '') {
    checkCancelled();
    if (!entry.name || /[\/\\]/.test(entry.name)) throw Error('文件名称无效');
    const path = prefix ? `${prefix}/${entry.name}` : entry.name;
    validate(path);
    if (entry.isFile) {
      const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
      checkCancelled();
      files.push({file, relativePath: path});
    } else if (entry.isDirectory) {
      directories.push(path);
      onDirectory(path);
      const reader = entry.createReader();
      while (true) {
        checkCancelled();
        const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
        if (!batch.length) break;
        for (const child of batch) await visit(child, path);
      }
    } else throw Error(`无法读取「${path}」，请检查本地访问权限`);
  }
  for (const root of roots) {
    checkCancelled();
    if (root.entry) await visit(root.entry);
    else {
      validate(root.file.name);
      files.push({file: root.file, relativePath: root.file.name});
    }
  }
  return {files, directories};
}
