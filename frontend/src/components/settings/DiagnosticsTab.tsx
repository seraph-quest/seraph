import { AuditLogPanel } from "./AuditLogPanel";
import { CapabilityChecksPanel } from "./CapabilityChecksPanel";
import { CordisRuntimePanel } from "./CordisRuntimePanel";
import { InferenceAccountingPanel } from "./InferenceAccountingPanel";
import { ModelRoutesPanel } from "./ModelRoutesPanel";
import { NearTeePanel } from "./NearTeePanel";
import { NearTextPanel } from "./NearTextPanel";
import { OpenRouterSetupPanel } from "./OpenRouterSetupPanel";
import { SettingsFold } from "./settingsLayout";
import { useArtifactStorageSettings, useModelFabric } from "./settingsData";

/**
 * The Diagnostics tab (#1280 PR 3): every raw readout and advanced control the
 * old first tab carried, plus the audit log, as closed folds. Nothing opens by
 * default; a deep link opens the fold it names. The controls inside are the
 * same components with the same save paths as before.
 */
export function DiagnosticsTab({ openAnchor }: { openAnchor?: string | null }) {
  const fabric = useModelFabric();
  const artifacts = useArtifactStorageSettings();
  const settings = fabric.modelFabric;
  const refresh = () => fabric.refresh();
  return (
    <div className="flex flex-col gap-2 px-1">
      <p className="text-[10px] text-retro-text/60">
        Raw status and advanced controls. Nothing here is needed for everyday use.
      </p>
      <SettingsFold anchor="runtime-host" title="Runtime host" openAnchor={openAnchor}>
        <CordisRuntimePanel managed metadata={fabric.cordisMetadata} />
      </SettingsFold>
      <SettingsFold anchor="model-routes" title="Model routes" openAnchor={openAnchor}>
        <ModelRoutesPanel
          settings={artifacts.settings}
          modelFabric={settings}
          modelFabricRuntime={fabric.runtime}
          modelFabricStale={fabric.stale}
          modelFabricError={fabric.error}
        />
      </SettingsFold>
      <SettingsFold anchor="near-slots" title="NEAR slots: text, screenshots, memory search" openAnchor={openAnchor}>
        <NearTeePanel
          setup={settings?.near_tee}
          stale={fabric.stale || Boolean(settings?.near_tee_metadata_unavailable)}
          onSave={fabric.save}
          onRefreshed={() => { void fabric.refresh(); }}
          policyRevision={settings?.egress_revision}
          policyRevoked={settings?.egress_revoked}
        />
      </SettingsFold>
      <SettingsFold anchor="openrouter" title="Retired OpenRouter" openAnchor={openAnchor}>
        <OpenRouterSetupPanel
          setup={settings?.openrouter_setup}
          stale={fabric.stale}
          onSave={fabric.save}
          policyRevision={settings?.egress_revision}
          policyRevoked={settings?.egress_revoked}
          retired={settings?.openrouter_retired ?? true}
        />
      </SettingsFold>
      <SettingsFold anchor="near-text" title="NEAR text (legacy)" openAnchor={openAnchor}>
        <NearTextPanel
          setup={settings?.near_text}
          stale={fabric.stale || Boolean(settings?.near_text_metadata_unavailable)}
          onSave={fabric.save}
          policyRevision={settings?.egress_revision}
          policyRevoked={settings?.egress_revoked}
        />
      </SettingsFold>
      <SettingsFold anchor="cost-ledger" title="Cost ledger" openAnchor={openAnchor}>
        <InferenceAccountingPanel accounting={settings?.inference_accounting} stale={fabric.stale} onRefresh={refresh} />
      </SettingsFold>
      <SettingsFold anchor="capability-checks" title="Capability checks" openAnchor={openAnchor}>
        <CapabilityChecksPanel modelFabric={settings} modelFabricStale={fabric.stale} onRefresh={refresh} />
      </SettingsFold>
      <SettingsFold anchor="audit" title="Audit log" openAnchor={openAnchor}>
        <AuditLogPanel />
      </SettingsFold>
    </div>
  );
}
