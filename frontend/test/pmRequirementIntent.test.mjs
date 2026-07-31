import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildRequirementRevisionOperations,
  completeRequirementAttachmentFrom,
  effectiveMessageIntent,
} from '../src/utils/pmRequirementIntent.ts';

const attachments = [
  { uid: 'a', name: 'one.md', digest: 'sha256:a', text: '附件一需求' },
  { uid: 'b', name: 'two.md', digest: 'sha256:b', text: '附件二需求' },
];

test('discussion text does not create canonical revision operations', () => {
  assert.deepEqual(
    buildRequirementRevisionOperations('discussion', '继续讨论', []),
    [],
  );
});

test('typed requirements and retained attachments become independent events', () => {
  assert.deepEqual(
    buildRequirementRevisionOperations('append', '新增约束', attachments),
    [
      { content: '新增约束', source: 'user', replace: false },
      { content: '附件一需求', source: 'attachment', replace: false },
      { content: '附件二需求', source: 'attachment', replace: false },
    ],
  );
});

test('attachments cannot silently remain discussion-only', () => {
  assert.equal(effectiveMessageIntent('discussion', attachments), 'append');
});

test('replace applies only to the complete typed snapshot before attachments', () => {
  const operations = buildRequirementRevisionOperations(
    'replace', '完整新需求', attachments.slice(0, 1),
  );
  assert.equal(operations[0].replace, true);
  assert.equal(operations[1].replace, false);
});

test('removed attachment is absent from revision operations', () => {
  const retained = attachments.filter(attachment => attachment.uid !== 'a');
  const operations = buildRequirementRevisionOperations(
    'append', '新增约束', retained,
  );
  assert.deepEqual(
    operations.map(operation => operation.content),
    ['新增约束', '附件二需求'],
  );
});

test('only complete authoritative upload extraction can be attached', () => {
  assert.equal(completeRequirementAttachmentFrom({
    complete: false,
    canonical_requirements_accepted: false,
    files: [{ complete: false, reason: 'pdf_text_limit_exceeded', text: 'partial' }],
  }), null);
  assert.deepEqual(completeRequirementAttachmentFrom({
    complete: true,
    canonical_requirements_accepted: true,
    files: [{ complete: true, filename: 'requirements.md', text: '完整需求' }],
  }), {
    name: 'requirements.md',
    text: '完整需求',
  });
});
