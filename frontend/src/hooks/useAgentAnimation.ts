import { useCallback, useEffect } from "react";
import { useChatStore } from "../stores/chatStore";
import { getToolTarget, getIdleState, getThinkingState } from "../lib/animationStateMachine";
import { EventBus } from "../game/EventBus";

export function useAgentAnimation() {
  const { agentVisual, setAgentVisual, resetAgentVisual } = useChatStore();

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
    (toolName: string) => {
      const target = getToolTarget(toolName);
      if (!target) return;

      setAgentVisual({
        animationState: target.animationState,
        positionX: target.positionX,
        speechText: null,
      });

      EventBus.emit("agent-move-to-tool", {
        tool: toolName,
        targetX: target.pixelX,
        targetY: target.pixelY,
        anim: target.animationState,
      });
    },
    [setAgentVisual]
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
