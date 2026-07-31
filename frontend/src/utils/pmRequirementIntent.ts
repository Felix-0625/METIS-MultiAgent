export type MessageIntent = 'discussion' | 'append' | 'replace';

export interface RequirementAttachment {
  uid: string;
  name: string;
  digest: string;
  text: string;
}

export interface RequirementRevisionOperation {
  content: string;
  source: 'user' | 'attachment';
  replace: boolean;
}

export interface CompleteAttachmentExtraction {
  name: string;
  text: string;
}

export const completeRequirementAttachmentFrom = (
  payload: any,
): CompleteAttachmentExtraction | null => {
  const extracted = Array.isArray(payload?.files) ? payload.files[0] : null;
  if (
    payload?.complete !== true
    || payload?.canonical_requirements_accepted !== true
    || extracted?.complete !== true
    || typeof extracted?.text !== 'string'
    || !extracted.text.trim()
  ) {
    return null;
  }
  return {
    name: String(extracted.filename || 'attachment'),
    text: extracted.text,
  };
};

export const effectiveMessageIntent = (
  intent: MessageIntent,
  attachments: RequirementAttachment[],
): MessageIntent => (
  attachments.length > 0 && intent === 'discussion' ? 'append' : intent
);

export const buildRequirementRevisionOperations = (
  intent: MessageIntent,
  typedText: string,
  attachments: RequirementAttachment[],
): RequirementRevisionOperation[] => {
  const effectiveIntent = effectiveMessageIntent(intent, attachments);
  if (effectiveIntent === 'discussion') return [];

  const operations: RequirementRevisionOperation[] = [{
    content: typedText.trim(),
    source: 'user',
    replace: effectiveIntent === 'replace',
  }];
  for (const attachment of attachments) {
    operations.push({
      content: attachment.text,
      source: 'attachment',
      replace: false,
    });
  }
  return operations.filter(operation => operation.content.length > 0);
};
