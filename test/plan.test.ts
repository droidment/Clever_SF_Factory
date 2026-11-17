import { describe, expect, it } from 'vitest';
import { validatePlan, type PlanNode } from '../src/engine/plan.ts';
import { templates, withRequiredGates } from '../src/policy.ts';
import { PermanentError } from '../src/types.ts';

const strict = { requireApproval: true };

describe('validatePlan', () => {
  it('accepts every template once policy has added its gates', () => {
    for (const build of Object.values(templates)) {
      expect(() => validatePlan(withRequiredGates(build()), strict)).not.toThrow();
    }
  });

  it('rejects a template that has no approval gate', () => {
    expect(() => validatePlan(templates.minor_task(), strict)).toThrow(PermanentError);
  });

  it('rejects an approval gate that does not wait for every task', () => {
    const nodes: PlanNode[] = [
      { key: 'implement', type: 'implement', dependsOn: [] },
      { key: 'ci', type: 'ci', dependsOn: ['implement'] },
      { key: 'gate', type: 'human_approval', dependsOn: ['implement'] },
    ];
    expect(() => validatePlan(nodes, strict)).toThrow(/after every other task/);
  });

  it('rejects duplicate keys, unknown dependencies and cycles', () => {
    expect(() =>
      validatePlan(
        [
          { key: 'a', type: 'ci', dependsOn: [] },
          { key: 'a', type: 'ci', dependsOn: [] },
        ],
        { requireApproval: false },
      ),
    ).toThrow(/duplicate/);
    expect(() =>
      validatePlan([{ key: 'a', type: 'ci', dependsOn: ['missing'] }], { requireApproval: false }),
    ).toThrow(/unknown task/);
    expect(() =>
      validatePlan(
        [
          { key: 'a', type: 'ci', dependsOn: ['b'] },
          { key: 'b', type: 'ci', dependsOn: ['a'] },
        ],
        { requireApproval: false },
      ),
    ).toThrow(/cycle/);
  });
});
