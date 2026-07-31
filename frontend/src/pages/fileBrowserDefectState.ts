export interface FileBrowserDefectState {
  status: string
  action_allowed?: boolean
  blocked_reason?: string
  identity_confidence?: string
  requires_identity_review?: boolean
}

export interface RepairApplyTarget {
  id: string
  file_path: string
}

export const canonicalUiPath = (value: string) =>
  String(value || '')
    .replace(/\\/g, '/')
    .replace(/^\.\/+/, '')
    .replace(/\/+/g, '/')
    .replace(/^\/+|\/+$/g, '')

export const canRepairDefect = (
  defect?: FileBrowserDefectState | null,
) => !!defect
  && defect.action_allowed === true
  && ['needs_manual', 'open'].includes(defect.status)
  && defect.identity_confidence === 'high'
  && defect.requires_identity_review !== true

export const defectStatusPresentation = (
  defects: FileBrowserDefectState[],
) => {
  const actionable = defects.find(canRepairDefect)
  if (actionable?.status === 'needs_manual') {
    return {
      color: 'orange',
      label: '需人工整改 · 点击整改',
      tooltip: '可提交人工整改方案',
    }
  }
  if (actionable?.status === 'open') {
    return {
      color: 'red',
      label: '待处理 · 点击整改',
      tooltip: '待处理缺陷，可提交整改方案',
    }
  }
  if (defects.some(
    defect => defect.requires_identity_review
      || defect.identity_confidence !== 'high'
      || defect.blocked_reason === 'identity_review_required',
  )) {
    return {
      color: 'red',
      label: '等待身份复核',
      tooltip: '低置信缺陷不可直接整改',
    }
  }
  if (defects.some(defect => defect.status === 'fixing')) {
    return {
      color: 'blue',
      label: '整改处理中',
      tooltip: '已有整改任务，不能重复提交',
    }
  }
  if (defects.some(defect => defect.status === 'pending_verification')) {
    return {
      color: 'gold',
      label: '等待权威复检',
      tooltip: '已写入整改，等待 Supervisor/Final QA',
    }
  }
  return {
    color: 'default',
    label: '当前不可整改',
    tooltip: '当前缺陷状态不可整改',
  }
}

export const countDefectFilesUnder = (
  defectPaths: Iterable<string>,
  directory: string,
) => {
  const prefix = `${canonicalUiPath(directory)}/`
  return new Set(
    [...defectPaths]
      .map(canonicalUiPath)
      .filter(path => path.startsWith(prefix)),
  ).size
}

export const buildRepairApplyTarget = (
  defect: RepairApplyTarget,
  confirmedPlanVersions: Record<string, string>,
) => ({
  defect_id: defect.id,
  file_path: canonicalUiPath(defect.file_path),
  confirmed_plan_version: confirmedPlanVersions[defect.id] || '',
})
