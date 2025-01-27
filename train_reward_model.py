#!python
# -*- coding: utf-8 -*-
# @author: Kun

import os
import torch
import evaluate
import numpy as np
import torch.nn as nn
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from datasets import load_dataset
from peft import LoraConfig, TaskType, get_peft_model, prepare_model_for_int8_training, prepare_model_for_kbit_training
import bitsandbytes as bnb
from transformers import (
    AutoConfig,
    AutoModelForSequenceClassification,
    AutoTokenizer,
    AutoModel,
    HfArgumentParser,
    PreTrainedTokenizerBase,
    Trainer,
    TrainingArguments,
    BitsAndBytesConfig
)
from transformers.utils import PaddingStrategy
from transformers import LlamaForSequenceClassification, LlamaConfig, LlamaTokenizer
from transformers import AutoModelForSeq2SeqLM , AutoModel

from reward_model import RewardModel

DEFAULT_PAD_TOKEN = "[PAD]"
DEFAULT_EOS_TOKEN = "</s>"
DEFAULT_BOS_TOKEN = "</s>"
DEFAULT_UNK_TOKEN = "</s>"


# Define and parse arguments.
@dataclass
class ScriptArguments:
    """
    These arguments vary depending on how many GPUs you have, what their capacity and features are, and what size model you want to train.
    """

    local_rank: Optional[int] = field(
        default=-1, metadata={"help": "Used for multi-gpu"})
    resume_from_checkpoint: Optional[bool] = field(
        default=False,
        metadata={"help": "If you want to resume training where it left off."},
    )
    deepspeed: Optional[str] = field(
        default=None,
        metadata={
            "help": "Path to deepspeed config if using deepspeed. You may need this if the model that you want to train doesn't fit on a single GPU."
        },
    )
    per_device_train_batch_size: Optional[int] = field(default=4)
    per_device_eval_batch_size: Optional[int] = field(default=1)
    gradient_accumulation_steps: Optional[int] = field(default=1)
    learning_rate: Optional[float] = field(default=2e-5)
    weight_decay: Optional[int] = field(default=0.001)
    model_name: Optional[str] = field(
        default="decapoda-research/llama-7b-hf",
        metadata={
            "help": "The model that you want to train from the Hugging Face hub or local."
        },
    )
    bf16: Optional[bool] = field(
        default=False,
        metadata={
            "help": "This essentially cuts the training time in half if you want to sacrifice a little precision and have a supported GPU."
        },
    )
    num_train_epochs: Optional[int] = field(
        default=1,
        metadata={"help": "The number of training epochs for the reward model."},
    )
    train_subset: Optional[int] = field(
        default=100000,
        metadata={"help": "The size of the subset of the training data to use"},
    )
    eval_subset: Optional[int] = field(
        default=50000,
        metadata={"help": "The size of the subset of the eval data to use"},
    )
    gradient_checkpointing: Optional[bool] = field(
        default=False,
        metadata={"help": "Enables gradient checkpointing."},
    )
    optim: Optional[str] = field(
        default="adamw_hf",
        metadata={"help": "Enables gradient checkpointing."},
    )
    lr_scheduler_type: Optional[str] = field(
        default="linear",
        metadata={"help": "The lr scheduler"},
    )


parser = HfArgumentParser(ScriptArguments)
script_args = parser.parse_args_into_dataclasses()[0]

dataset_name = "./datasets/"
print("dataset_name: ", dataset_name)

# Load the dataset for tuning the reward model.
# train_dataset = load_dataset("lvwerra/stack-exchange-paired", data_dir="data/reward", split="train")
train_dataset = load_dataset(dataset_name, split="train")
if script_args.train_subset > 0:
    train_dataset = train_dataset.select(range(script_args.train_subset))
# eval_dataset = load_dataset("lvwerra/stack-exchange-paired", data_dir="data/evaluation", split="train")
eval_dataset = load_dataset(dataset_name, split="train")
if script_args.eval_subset > 0:
    eval_dataset = eval_dataset.select(range(script_args.eval_subset))
# Define the training args. Needs to be done before the model is loaded if you are using deepspeed.
model_name_split = script_args.model_name.split("/")[-1]
# output_name = (
#     f"{model_name_split}_peft_gpt-4-llm_rm_{script_args.train_subset}_{script_args.learning_rate}"
# )
# output_name = (
#     f"{model_name_split}_peft_comparision_data-paired_rmts__{script_args.train_subset}_{script_args.learning_rate}"
# )
output_name = (
    f"reward_model_{model_name_split}__{script_args.train_subset}_{script_args.learning_rate}"
)

training_args = TrainingArguments(
    output_dir=output_name,
    learning_rate=script_args.learning_rate,
    per_device_train_batch_size=script_args.per_device_train_batch_size,
    per_device_eval_batch_size=script_args.per_device_eval_batch_size,
    num_train_epochs=script_args.num_train_epochs,
    weight_decay=script_args.weight_decay,
    evaluation_strategy="steps",
    eval_steps=200,  # 500,
    save_strategy="steps",
    save_steps=200,  # 500,
    save_total_limit=2,
    gradient_accumulation_steps=script_args.gradient_accumulation_steps,
    gradient_checkpointing=script_args.gradient_checkpointing,
    deepspeed=script_args.deepspeed,
    # local_rank=script_args.local_rank,
    remove_unused_columns=False,
    label_names=[],
    # bf16=script_args.bf16,
    # fp16=True, #! this is important! if True, cuda out of memory.
    logging_strategy="steps",
    logging_steps=2,
    optim=script_args.optim,
    lr_scheduler_type=script_args.lr_scheduler_type,
    report_to =["tensorboard"]
)

# Load the value-head model and tokenizer.
# tokenizer = AutoTokenizer.from_pretrained(script_args.model_name, use_auth_token=True)
if "llama" in script_args.model_name or "vicuna" in script_args.model_name or "Vicuna" in script_args.model_name:
    tokenizer = LlamaTokenizer.from_pretrained(script_args.model_name)
    config = LlamaConfig.from_pretrained(script_args.model_name)

elif "chatglm" in script_args.model_name:
    tokenizer = AutoTokenizer.from_pretrained(
        script_args.model_name, trust_remote_code=True)
    config = AutoConfig.from_pretrained(
        script_args.model_name, trust_remote_code=True)
    
else:
    tokenizer = AutoTokenizer.from_pretrained(
        script_args.model_name, trust_remote_code=True)
    config = AutoConfig.from_pretrained(
        script_args.model_name, trust_remote_code=True)

print("tokenizer: ", type(tokenizer)) 

if "llama" in script_args.model_name or "vicuna" in script_args.model_name or "Vicuna" in script_args.model_name:
    # required for llama
    tokenizer.add_special_tokens(
        {
            "eos_token": DEFAULT_EOS_TOKEN,
            "bos_token": DEFAULT_BOS_TOKEN,
            "unk_token": DEFAULT_UNK_TOKEN,
            "pad_token": DEFAULT_PAD_TOKEN,
        }
    )
else:
    # required for gpt2
    #tokenizer.pad_token = tokenizer.eos_token
    print(f"tokenizer.pad_token={tokenizer.pad_token}")

device_map = "auto"
world_size = int(os.environ.get("WORLD_SIZE", 1))
ddp = world_size != 1
if ddp:
    device_map = {"": int(os.environ.get("LOCAL_RANK") or 0)}
print("device_map: ", device_map)
# model = AutoModelForSequenceClassification.from_pretrained(
#    script_args.model_name, num_labels=1, torch_dtype=torch.bfloat16
# )

if "llama" in script_args.model_name or "vicuna" in script_args.model_name or "Vicuna" in script_args.model_name:
    model = LlamaForSequenceClassification.from_pretrained(
        script_args.model_name,
        num_labels=1,
        # torch_dtype=torch.bfloat16,
        torch_dtype=torch.float16,
        load_in_8bit=True,
        device_map=device_map,
    )
elif "chatglm" in script_args.model_name:
    q_config = BitsAndBytesConfig(load_in_4bit= True,
                                  bnb_4bit_quant_type='nf4',
                                  bnb_4bit_use_double_quant=True,
                                  bnb_4bit_compute_dtype=torch.float16)
    # model = AutoModelForSeq2SeqLM.from_pretrained(
    #     script_args.model_name,
    #     num_labels=1,
    #     # torch_dtype=torch.bfloat16,
    #     torch_dtype=torch.float16,
    #     trust_remote_code=True,
    #     load_in_4bit=True,
    #     device_map=device_map,
    #     quantization_config=q_config,
    # )
    model = AutoModel.from_pretrained(
        script_args.model_name,
        #num_labels=1,
        # torch_dtype=torch.bfloat16,
        torch_dtype=torch.float16,
        trust_remote_code=True,
        load_in_4bit=True,
        device_map=device_map,
        quantization_config=q_config,
    )
else:
    model = AutoModelForSequenceClassification.from_pretrained(
        script_args.model_name,
        num_labels=1,
        # torch_dtype=torch.bfloat16,
        torch_dtype=torch.float32,
        trust_remote_code=True,
        load_in_8bit=True,
        device_map=device_map,
        
    )

print("model: ", type(model))

model = prepare_model_for_kbit_training(model)
print(f'memory footprint of model: {model.get_memory_footprint()/(1024*1024*1024)} GB')
print("model: ", type(model))
def find_all_linear_names(model):
    """
    找出所有全连接层，为所有全连接添加adapter
    """
    cls = bnb.nn.Linear4bit
    lora_module_names = set()
    for name, module in model.named_modules():
        if isinstance(module, cls):
            names = name.split('.')
            lora_module_names.add(names[0] if len(names) == 1 else names[-1])

    if 'lm_head' in lora_module_names:  # needed for 16-bit
        lora_module_names.remove('lm_head')
    if  'output_layer' in lora_module_names:
        lora_module_names.remove('output_layer')
    return list(lora_module_names)

target_modules = find_all_linear_names(model)
peft_config = LoraConfig(
    task_type=TaskType.SEQ_CLS,
    inference_mode=False,
    target_modules = target_modules ,
    r=64,  # for qlora 64 is ok
    lora_alpha=16,  # 32,
    lora_dropout=0.05,  # 0.1,
    bias="none",
)

model = get_peft_model(model, peft_config)

model.print_trainable_parameters()

# Need to do this for gpt2, because it doesn't have an official pad token.
#tokenizer.pad_token = tokenizer.eos_token
#model.config.pad_token_id = tokenizer.eos_token_id
#model.config.use_cache = not script_args.gradient_checkpointing
num_proc = 1  # Can adjust to be higher if you have more processors.
original_columns = train_dataset.column_names

reward_model = RewardModel(model.config, model.transformer, tokenizer)
print(reward_model)
#layers = reward_model.transformer.layers
# Freeze the first 70% of the hidden layers of the reward model backbone
# parser.add_argument("--freeze_ratio", type=float, default=0.0, help="ratio of layers frozen for reward training")
#num_layers = len(layers)
#num_frozen = int(0.7 * num_layers)
#for layer in layers[:num_frozen]:
#    layer.requires_grad_(False)

# if args.checkpoint is not None:
#     checkpoints = glob.glob(args.checkpoint.replace("star", "*"))
#     st = dict()
#     for checkpoint in checkpoints:
#         st.update(torch.load(checkpoint, map_location="cpu"))
#     res = reward_model.load_state_dict(st, strict=False)
print(f"Finished loading model and tokenizer")

# Turn the dataset into pairs of post + summaries, where text_j is the preferred question + answer and text_k is the other.
# Then tokenize the dataset.
def preprocess_function(examples):
    new_examples = {
        "input_ids_j": [],
        "attention_mask_j": [],
        "input_ids_k": [],
        "attention_mask_k": [],
    }
    # for question, response_j, response_k in zip(examples["question"], examples["response_j"], examples["response_k"]):
    for question, response_j, response_k in zip(examples["user_input"], examples["completion_a"], examples["completion_b"]):
        tokenized_j = tokenizer(
            "Question: " + question + "\n\nAnswer: " + response_j, truncation=True)
        tokenized_k = tokenizer(
            "Question: " + question + "\n\nAnswer: " + response_k, truncation=True)

        new_examples["input_ids_j"].append(tokenized_j["input_ids"])
        new_examples["attention_mask_j"].append(tokenized_j["attention_mask"])
        new_examples["input_ids_k"].append(tokenized_k["input_ids"])
        new_examples["attention_mask_k"].append(tokenized_k["attention_mask"])

    return new_examples


# preprocess the dataset and filter out QAs that are longer than 512
print("train_dataset: ", len(train_dataset))
train_dataset = train_dataset.map(
    preprocess_function, batched=True, num_proc=num_proc, remove_columns=original_columns
)
train_dataset = train_dataset.filter(lambda x: len(
    x["input_ids_j"]) <= 512 and len(x["input_ids_k"]) <= 512)
print("train_dataset: ", len(train_dataset))

print("eval_dataset: ", len(eval_dataset))
eval_dataset = eval_dataset.map(
    preprocess_function, batched=True, num_proc=num_proc, remove_columns=original_columns)
eval_dataset = eval_dataset.filter(lambda x: len(
    x["input_ids_j"]) <= 512 and len(x["input_ids_k"]) <= 512)
print("eval_dataset: ", len(eval_dataset))

# We need to define a special data collator that batches the data in our j vs k format.
@dataclass
class RewardDataCollatorWithPadding:
    tokenizer: PreTrainedTokenizerBase
    padding: Union[bool, str, PaddingStrategy] = True
    max_length: Optional[int] = None
    pad_to_multiple_of: Optional[int] = None
    return_tensors: str = "pt"

    def __call__(self, features: List[Dict[str, Any]]) -> Dict[str, Any]:
        features_j = []
        features_k = []
        for feature in features:
            features_j.append(
                {
                    "input_ids": feature["input_ids_j"],
                    "attention_mask": feature["attention_mask_j"],
                }
            )
            features_k.append(
                {
                    "input_ids": feature["input_ids_k"],
                    "attention_mask": feature["attention_mask_k"],
                }
            )
        batch_j = self.tokenizer.pad(
            features_j,
            padding=self.padding,
            max_length=self.max_length,
            pad_to_multiple_of=self.pad_to_multiple_of,
            return_tensors=self.return_tensors,
        )
        batch_k = self.tokenizer.pad(
            features_k,
            padding=self.padding,
            max_length=self.max_length,
            pad_to_multiple_of=self.pad_to_multiple_of,
            return_tensors=self.return_tensors,
        )
        batch = {
            "input_ids_j": batch_j["input_ids"],
            "attention_mask_j": batch_j["attention_mask"],
            "input_ids_k": batch_k["input_ids"],
            "attention_mask_k": batch_k["attention_mask"],
            "return_loss": True,
        }
        return batch


# Define the metric that we'll use for validation.
accuracy = evaluate.load("accuracy")


def compute_metrics(eval_pred):
    predictions, _ = eval_pred
    # Here, predictions is rewards_j and rewards_k.
    # We want to see how much of the time rewards_j > rewards_k.
    predictions = np.argmax(predictions, axis=0)
    labels = np.zeros(predictions.shape)
    return accuracy.compute(predictions=predictions, references=labels)


class RewardTrainer(Trainer):
    # Define how to compute the reward loss. We use the InstructGPT pairwise logloss: https://arxiv.org/abs/2203.02155
    def compute_loss(self, model, inputs, return_outputs=False):
        # print('inputs["input_ids_j"]: ', inputs["input_ids_j"].shape)
        # print('inputs["attention_mask_j"]: ', inputs["attention_mask_j"].shape)
        rewards_j = model(
            chosen_input_ids=inputs["input_ids_j"], chosen_attention_mask=inputs["attention_mask_j"])["chosen_reward"]
        # print("rewards_j: ", type(rewards_j), rewards_j.shape)

        # print('inputs["input_ids_k"]: ', inputs["input_ids_k"].shape)
        # print('inputs["attention_mask_k"]: ', inputs["attention_mask_k"].shape)
        rewards_k = model(
            rejected_input_ids=inputs["input_ids_k"], rejected_attention_mask=inputs["attention_mask_k"])["reject_reward"]
        # print("rewards_k: ", type(rewards_k), rewards_k.shape)
        
        loss = -nn.functional.logsigmoid(rewards_j - rewards_k).mean()
        if return_outputs:
            return loss, {"rewards_j": rewards_j, "rewards_k": rewards_k}
        return loss

    def save_model(self, output_dir: Optional[str] = None, _internal_call: bool = False):
        """只保存adapter"""
        print("begin to save  !!!")
        if output_dir is None:
            output_dir = self.args.output_dir
        if self.is_world_process_zero():  
            self.model.save_pretrained(output_dir)
            torch.save(self.args, os.path.join(output_dir, "training_args.bin"))
            print("save done !!!")
        else :
            print("this process is not main process , do not save model.[for distributed training scenario]")


# Train the model, woohoo.
trainer = RewardTrainer(
    # model=model,
    model=reward_model,
    args=training_args,
    train_dataset=train_dataset,
    eval_dataset=eval_dataset,
    compute_metrics=compute_metrics,
    data_collator=RewardDataCollatorWithPadding(
        tokenizer=tokenizer, max_length=512, pad_to_multiple_of=8),
    
)

model.config.use_cache = False

trainer.train(script_args.resume_from_checkpoint)

print("Saving last checkpoint of the model")
# model.save_pretrained(script_args.output_dir + "peft_last_checkpoint")
model.save_pretrained(output_name)
