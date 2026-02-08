import Phaser from "phaser";
import { EventBus } from "../EventBus";
import { RoomBackground } from "../objects/RoomBackground";
import { Furniture } from "../objects/Furniture";
import { AgentSprite } from "../objects/AgentSprite";
import { SpeechBubble } from "../objects/SpeechBubble";
import { SCENE } from "../../config/constants";

interface ToolMovePayload {
  tool: string;
  targetX: number;
  targetY: number;
  anim: string;
}

interface FinalAnswerPayload {
  text: string;
}

export class StudyScene extends Phaser.Scene {
  private agent!: AgentSprite;
  private speechBubble!: SpeechBubble;

  // Store bound handlers for cleanup
  private handleThink!: () => void;
  private handleToolMove!: (payload: ToolMovePayload) => void;
  private handleFinalAnswer!: (payload: FinalAnswerPayload) => void;
  private handleReturnIdle!: () => void;

  constructor() {
    super("StudyScene");
  }

  preload() {
    AgentSprite.preload(this);
  }

  create() {
    new RoomBackground(this);
    new Furniture(this);

    const startPos = SCENE.POSITIONS.desk;
    this.agent = new AgentSprite(this, startPos.x, startPos.y);
    this.speechBubble = new SpeechBubble(this);
    this.speechBubble.setTarget(this.agent.sprite);

    // Bind EventBus handlers
    this.handleThink = () => {
      this.speechBubble.hide();
      const pos = SCENE.POSITIONS.desk;
      this.agent.moveTo(pos.x, pos.y, () => {
        this.agent.playAnim("think");
      });
    };

    this.handleToolMove = (payload: ToolMovePayload) => {
      this.speechBubble.hide();
      this.agent.moveTo(payload.targetX, payload.targetY, () => {
        this.agent.playAnim(payload.anim);
      });
    };

    this.handleFinalAnswer = (payload: FinalAnswerPayload) => {
      const pos = SCENE.POSITIONS.desk;
      this.agent.moveTo(pos.x, pos.y, () => {
        this.agent.playAnim("idle");
        this.speechBubble.show(payload.text);

        this.time.delayedCall(3000, () => {
          this.speechBubble.hide();
          EventBus.emit("agent-speech-done");
        });
      });
    };

    this.handleReturnIdle = () => {
      this.speechBubble.hide();
      const pos = SCENE.POSITIONS.desk;
      this.agent.moveTo(pos.x, pos.y, () => {
        this.agent.playAnim("idle");
      });
    };

    EventBus.on("agent-think", this.handleThink);
    EventBus.on("agent-move-to-tool", this.handleToolMove);
    EventBus.on("agent-final-answer", this.handleFinalAnswer);
    EventBus.on("agent-return-idle", this.handleReturnIdle);

    EventBus.emit("current-scene-ready", this);
  }

  update() {
    this.speechBubble.updatePosition();
  }

  shutdown() {
    EventBus.off("agent-think", this.handleThink);
    EventBus.off("agent-move-to-tool", this.handleToolMove);
    EventBus.off("agent-final-answer", this.handleFinalAnswer);
    EventBus.off("agent-return-idle", this.handleReturnIdle);

    this.agent.destroy();
    this.speechBubble.destroy();
  }
}
