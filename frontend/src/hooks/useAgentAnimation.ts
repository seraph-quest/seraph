import { useCallback, useEffect } from "react";
import { useChatStore } from "../stores/chatStore";
import { getToolEffect, getIdleState, getThinkingState } from "../lib/animationStateMachine";
import { EventBus } from "../game/EventBus";

export function useAgentAnimation() {
  const { agentVisual, setAgentVisual, resetAgentVisual, magicEffectPoolSize, setMagicEffectPoolSize } = useChatStore();

  // Listen for magic effects loaded from VillageScene
  useEffect(() => {
    const onEffectsLoaded = (poolSize: number) => {
      setMagicEffectPoolSize(poolSize);
    };
    EventBus.on("magic-effects-loaded", onEffectsLoaded);
    return () => {
      EventBus.off("magic-effects-loaded", onEffectsLoaded);
    };
  }, [setMagicEffectPoolSize]);

  const onThinking = useCallback(() => {
    const thinking = getThinkingState();
    setAgentVisual({
      animationState: "thinking",
      positionX: thinking.positionX,
      speechText: null,
    });
    EventBus.emit("agent-think");
  }, [setAgentVisual]);

  const onToolDetected = useCallback(
    (toolName: string, stepContent?: string) => {
      const effect = getToolEffect(toolName, magicEffectPoolSize);
      if (!effect) {
        // No magic effects available — stay in thinking state
        return;
      }

      setAgentVisual({
        animationState: effect.animationState,
        speechText: stepContent ?? null,
      });

      EventBus.emit("agent-cast-effect", {
        tool: toolName,
        effectIndex: effect.effectIndex,
        text: stepContent ?? "",
      });
    },
    [setAgentVisual, magicEffectPoolSize]
  );

  const onFinalAnswer = useCallback(
    (answer: string) => {
      const idle = getIdleState();
      const truncated =
        answer.length > 80 ? answer.slice(0, 80) + "..." : answer;

      setAgentVisual({
        animationState: "speaking",
        positionX: idle.positionX,
        speechText: truncated,
      });

      EventBus.emit("agent-final-answer", { text: answer });
    },
    [setAgentVisual]
  );

  const returnToIdle = useCallback(() => {
    resetAgentVisual();
    EventBus.emit("agent-return-idle");
  }, [resetAgentVisual]);

  // Listen for speech done from Phaser scene
  useEffect(() => {
    const onSpeechDone = () => {
      resetAgentVisual();
    };
    EventBus.on("agent-speech-done", onSpeechDone);
    return () => {
      EventBus.off("agent-speech-done", onSpeechDone);
    };
  }, [resetAgentVisual]);

  return {
    currentPosition: agentVisual.positionX,
    facingDirection: agentVisual.facing,
    animationState: agentVisual.animationState,
    onThinking,
    onToolDetected,
    onFinalAnswer,
    returnToIdle,
  };
}
