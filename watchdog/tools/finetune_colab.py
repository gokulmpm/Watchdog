"""
Fine-tune Qwen2.5-3B for foundry alert analysis using Unsloth + QLoRA.

HOW TO USE:
  1. Go to https://colab.research.google.com
  2. Runtime -> Change runtime type -> T4 GPU (free)
  3. Paste this entire file into a code cell and run it
  4. Upload your training_data.jsonl when prompted
  5. Download the exported model at the end

Total time: ~1-2 hours on free Colab T4 GPU
"""

# ── Cell 1: Install dependencies ──────────────────────────────────────────────
# Run this cell first and restart runtime when prompted

INSTALL = """
%%bash
pip install "unsloth[colab-new] @ git+https://github.com/unslothai/unsloth.git" -q
pip install --no-deps trl peft accelerate bitsandbytes xformers -q
"""

# ── Cell 2: Fine-tuning script ─────────────────────────────────────────────────

FINETUNE_SCRIPT = '''
import json
from datasets import Dataset
from unsloth import FastLanguageModel
from trl import SFTTrainer
from transformers import TrainingArguments
from google.colab import files

# ── 1. Load model ──────────────────────────────────────────────────────────────
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name  = "Qwen/Qwen2.5-3B-Instruct",
    max_seq_length = 2048,
    dtype          = None,   # auto
    load_in_4bit   = True,   # QLoRA
)

model = FastLanguageModel.get_peft_model(
    model,
    r              = 16,    # LoRA rank — higher = more params, better quality
    target_modules = ["q_proj", "k_proj", "v_proj", "o_proj",
                      "gate_proj", "up_proj", "down_proj"],
    lora_alpha     = 16,
    lora_dropout   = 0,
    bias           = "none",
    use_gradient_checkpointing = "unsloth",
    random_state   = 42,
)

# ── 2. Load training data ──────────────────────────────────────────────────────
print("Upload your training_data.jsonl file:")
uploaded = files.upload()
filename = list(uploaded.keys())[0]

rows = []
with open(filename, encoding="utf-8") as f:
    for line in f:
        rows.append(json.loads(line))
print(f"Loaded {len(rows)} training examples")

# ── 3. Format as chat template ─────────────────────────────────────────────────
def format_example(row):
    messages = [
        {"role": "system",    "content": row["instruction"]},
        {"role": "user",      "content": row["input"]},
        {"role": "assistant", "content": row["output"]},
    ]
    return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)

dataset = Dataset.from_list([{"text": format_example(r)} for r in rows])
print("Dataset sample:")
print(dataset[0]["text"][:500])

# ── 4. Train ───────────────────────────────────────────────────────────────────
trainer = SFTTrainer(
    model     = model,
    tokenizer = tokenizer,
    train_dataset = dataset,
    dataset_text_field = "text",
    max_seq_length     = 2048,
    dataset_num_proc   = 2,
    args = TrainingArguments(
        per_device_train_batch_size = 2,
        gradient_accumulation_steps = 4,
        warmup_steps    = 10,
        max_steps       = 200,          # ~30 min on T4; increase to 500 for better quality
        learning_rate   = 2e-4,
        fp16            = True,
        logging_steps   = 10,
        optim           = "adamw_8bit",
        weight_decay    = 0.01,
        lr_scheduler_type = "linear",
        seed            = 42,
        output_dir      = "./output",
    ),
)

trainer_stats = trainer.train()
print(f"Training loss: {trainer_stats.training_loss:.4f}")

# ── 5. Test the fine-tuned model ───────────────────────────────────────────────
FastLanguageModel.for_inference(model)

test_input = """Alert level: WARNING  |  SI score: 72.4/100

Non-stable parameters:
  Active Clay:  CRITICAL  drift=STRONG DRIFT  value=7.8  Δ=-4.2%  (Deviated LOW -0.65 (LCL=8.00))
  Moisture:  WATCH  var=ELEVATED  value=3.6  Δ=+1.8%"""

messages = [
    {"role": "system", "content": rows[0]["instruction"]},
    {"role": "user",   "content": test_input},
]
inputs = tokenizer.apply_chat_template(
    messages, tokenize=True, add_generation_prompt=True, return_tensors="pt"
).to("cuda")

outputs = model.generate(input_ids=inputs, max_new_tokens=200, temperature=0.3)
print("\\nModel output:")
print(tokenizer.decode(outputs[0][inputs.shape[1]:], skip_special_tokens=True))

# ── 6. Export to GGUF for Ollama ───────────────────────────────────────────────
print("\\nExporting to GGUF (Q4_K_M — best quality/size tradeoff)...")
model.save_pretrained_gguf(
    "foundry-alert-3b",
    tokenizer,
    quantization_method = "q4_k_m",   # ~1.8GB file
)

# Download the model
print("Downloading model file...")
files.download("foundry-alert-3b-unsloth.Q4_K_M.gguf")
print("\\nDone! Now install Ollama and run:")
print("  ollama create foundry-alert -f Modelfile")
'''

# ── Cell 3: Ollama Modelfile ───────────────────────────────────────────────────
MODELFILE = '''
# Save this as: Modelfile
# Then run:    ollama create foundry-alert -f Modelfile
#              ollama run foundry-alert

FROM ./foundry-alert-3b-unsloth.Q4_K_M.gguf

SYSTEM """
You are an expert in foundry sand preparation monitoring. You receive alert data
from a Sand Index (SI) monitoring system and produce a concise diagnosis.

Domain knowledge:
- Sand properties: active_clay, compactibility, GCS, GFN/AFS, moisture,
  permeability, LOI, volatile_matter, inert_fines, shear_strength, split_strength.
- Additives: bentonite (raises active clay), coal dust/LCA (raises LOI/volatile matter),
  fresh silica sand (controls GFN), water (affects moisture/compactibility).
- Drift = sustained multi-shift trend. Variance = batch-to-batch instability.
- Deviation = outside LCL/UCL control limits.

Respond ONLY with a JSON object:
{"root_cause": "...", "recommendation": "..."}
"""

PARAMETER temperature 0.3
PARAMETER num_ctx 2048
'''

# ── Cell 4: Wire Ollama into llm_analysis.py ──────────────────────────────────
OLLAMA_CONFIG = '''
# Add to watchdog_config.json:
{
  "llm_analysis": {
    "enabled": true,
    "provider": "ollama",
    "ollama_model": "foundry-alert",
    "ollama_url": "http://localhost:11434"
  }
}
'''

if __name__ == "__main__":
    print("=" * 60)
    print("FOUNDRY ALERT FINE-TUNING GUIDE")
    print("=" * 60)
    print()
    print("Step 1: Generate training data")
    print("  python -m watchdog.tools.generate_training_data --limit 500")
    print()
    print("Step 2: Go to colab.research.google.com")
    print("  - Runtime -> T4 GPU (free)")
    print("  - Paste the INSTALL cell, run it, restart runtime")
    print("  - Paste the FINETUNE_SCRIPT cell, run it")
    print("  - Download the .gguf file when it finishes")
    print()
    print("Step 3: Install Ollama (ollama.com) and run:")
    print("  ollama create foundry-alert -f Modelfile")
    print()
    print("Step 4: Update watchdog_config.json:")
    print(OLLAMA_CONFIG)
    print()
    print("Modelfile contents:")
    print(MODELFILE)
