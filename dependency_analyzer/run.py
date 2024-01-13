# Load model directly
import os
import torch
import json
import numpy as np
import glob
from io import open
import torch.nn as nn
from tqdm import tqdm
from torch.optim import AdamW
from torch.utils.data import DataLoader, TensorDataset
from transformers import AutoTokenizer, RobertaForTokenClassification, RobertaConfig
import torch.nn.functional as F
from sklearn.metrics import f1_score, precision_score, recall_score, accuracy_score
import datetime

class Model(nn.Module):
    def __init__(self, model_name):
        super(Model, self).__init__()
        
        # Define the layers
        self.config = RobertaConfig.from_pretrained(model_name)
        self.lm = RobertaForTokenClassification.from_pretrained(model_name)
        self.linear = nn.Sequential(
            nn.Linear(1024, 128),
            nn.ReLU(),
            nn.BatchNorm1d(128),   # Batch normalization layer
            nn.Linear(128, 16),
            nn.ReLU(),
            nn.BatchNorm1d(16),   # Batch normalization layer
            nn.Linear(16, 2)
        )

    
    def forward(self, input_ids, attention_mask):
        out = self.lm(input_ids=input_ids, attention_mask=attention_mask).logits
        out = out.flatten(start_dim=1)
        out = self.linear(out)
        return out
    
def load_data(filepath, tokenizer, max_length=512):
    encoded_data = []
    attention_mask = []
    labels = []
    with open(filepath, 'r') as f:
        dataset = json.load(f)
        for sample in tqdm(dataset): 
            input = sample[0][0] + '</s>' + sample[0][1]
            output = sample[1]

            # 使用tokenizer进行编码
            encoded_code = tokenizer.tokenize(input)[:max_length-2]
            encoded_code =[tokenizer.cls_token]+encoded_code+[tokenizer.sep_token]
            encoded_code =  tokenizer.convert_tokens_to_ids(encoded_code)
            source_mask = [1] * (len(encoded_code))
            padding_length = max_length - len(encoded_code)
            encoded_code+=[tokenizer.pad_token_id]*padding_length
            source_mask+=[0]*padding_length

            # 添加到encoded_data列表和labels列表
            encoded_data.append(encoded_code)
            attention_mask.append(source_mask)
            labels.append(output)

    # 组成 batch
    code_batch_tensor = torch.tensor(encoded_data, dtype=torch.long)
    attention_mask = torch.tensor(attention_mask, dtype=torch.long)
    labels_tensor = torch.tensor(labels, dtype=torch.float)
    
    # 创建TensorDataset
    dataset = TensorDataset(code_batch_tensor, attention_mask, labels_tensor)

    return dataset

def evaluate(model, dataloader, criterion, device, mode='dev'):
    model.eval()
    total_loss = 0
    gts = []
    preds = []

    with torch.no_grad():
        for batch in tqdm(dataloader):
            batch_input, attention_mask, batch_labels = [item.to(device) for item in batch]
            outputs = model(input_ids=batch_input, attention_mask=attention_mask)
            preds.append(outputs.detach().cpu())
            gts.append(batch_labels.detach().cpu())

            loss = criterion(outputs, batch_labels)
            total_loss += loss.item()

    gts = torch.cat(gts, dim=0)
    preds = torch.cat(preds, dim=0)

    result = np.where(np.array(preds) >= 0.5, 1, 0)
    
    acc = accuracy_score(gts[:,0], result[:,0])
    prec = precision_score(gts[:,0], result[:,0])
    reca = recall_score(gts[:,0], result[:,0])
    f1 = f1_score(gts[:,0], result[:,0])
    print(f"mode: {mode}, A depend on B\nacc: {acc}, precision: {prec}, recall: {reca}, F1: {f1}")

    acc = accuracy_score(gts[:,1], result[:,1])
    prec = precision_score(gts[:,1], result[:,1])
    reca = recall_score(gts[:,1], result[:,1])
    f1 = f1_score(gts[:,1], result[:,1])
    print(f"mode: {mode}, B depend by A\nacc: {acc}, precision: {prec}, recall: {reca}, F1: {f1}")
    
    if mode == 'dev':
        return total_loss / len(dataloader)
    else:
        return total_loss / len(dataloader)

def train_on_single_lang(model_name, lang, batch_size, train, test, checkpoint_path, epoch_num=10):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    
    # 初始化tokenizer和模型
    tokenizer = AutoTokenizer.from_pretrained(model_name,model_max_length=512)
    model = Model(model_name)
    model.to(device)

    # 初始化优化器和损失函数
    optimizer = AdamW(model.parameters(), lr=1e-5)
    criterion = nn.BCEWithLogitsLoss() #F1Loss()

    start_epoch = 0

    # 断点续训
    if os.path.exists(f'./model/{lang}') == False:
        os.makedirs(f'./model/{lang}')
    if not checkpoint_path:
        checkpoint_path = f'./model/{lang}/*.bin'   # globbing by default
    if os.path.exists(checkpoint_path):
        print('Loading checkpoint...')
        checkpoint = torch.load(checkpoint_path)
        model.load_state_dict(checkpoint['model_state_dict'])
        # optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        start_epoch = checkpoint['epoch']
    else:
        bin_files = glob.glob(checkpoint_path)
        if len(bin_files) > 0:
            checkpoint_path = bin_files[0]
            print(f'Using detected checkpoint file: {checkpoint_path}')
        else:
            print('No checkpoint found. Training from scratch...')

    # 加载数据
    if train:
        training_set = load_data(f'./dataset/{lang}/train.json', tokenizer)
        dev_set = load_data(f'./dataset/{lang}/valid.json', tokenizer)
        test_set = load_data(f'./dataset/{lang}/test.json', tokenizer)

        # 创建DataLoader
        train_dataloader = DataLoader(training_set, batch_size=batch_size, shuffle=True)
        dev_dataloader = DataLoader(dev_set, batch_size=batch_size)
        test_dataloader = DataLoader(test_set, batch_size=batch_size)

        # 训练模型
        model.train()

        preds = []
        step = 0
        for epoch in range(start_epoch, epoch_num):
            save_checkpoint_path = datetime.datetime.now().strftime(f'./model/{lang}/model_{lang}_%Y%m%d_%H%M%S_epoch{epoch}.bin')

            epoch_iterator = tqdm(train_dataloader, desc="Training", position=0, leave=True)
            for batch in epoch_iterator:
                step += 1
                batch_input, attention_mask, batch_labels = [item.to(device) for item in batch]

                optimizer.zero_grad()
                outputs = model(input_ids=batch_input, attention_mask=attention_mask)
                loss = criterion(outputs, batch_labels)
                loss.backward()
                optimizer.step()
                
                epoch_iterator.set_postfix({'loss': loss.item()}, refresh=True)
                if step % 1000 == 0:
                    torch.save({
                        'epoch': epoch + 1,
                        'model_state_dict': model.state_dict()
                        # 'optimizer_state_dict': optimizer.state_dict()
                    }, save_checkpoint_path)

            # print(f"Epoch {epoch+1} completed.")

            # 保存模型
            torch.save({
                'epoch': epoch + 1,
                'model_state_dict': model.state_dict()
                # 'optimizer_state_dict': optimizer.state_dict()
            }, save_checkpoint_path)
            
            # 每2个epochs做一次验证
            if (epoch + 1) % 2 == 0:
                val_loss = evaluate(model, dev_dataloader, criterion, device)
                print(f"Validation Loss: {val_loss}")
                # test_loss = evaluate(model, test_dataloader, criterion, device, mode='test')
                # print(f"Test Loss: {test_loss}")


    if test:
        # 测试
        test_set = load_data(f'./dataset/{lang}/test.json', tokenizer)
        test_dataloader = DataLoader(test_set, batch_size=batch_size)
        test_loss = evaluate(model, test_dataloader, criterion, device, mode='test')
        print(f"Test Loss: {test_loss}")

def run_test(model_checkpoint, lang, model_name, batch_size):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    
    tokenizer = AutoTokenizer.from_pretrained(model_name,model_max_length=512)
    model = Model(model_name)
    model.to(device)
    criterion = nn.BCEWithLogitsLoss() #F1Loss()
    
    if os.path.exists(model_checkpoint):
        print('Loading checkpoint...')
        checkpoint = torch.load(model_checkpoint)
        model.load_state_dict(checkpoint['model_state_dict'])
    else:
        print('No checkpoint found. Test aborted.')
        return

    test_set = load_data(f'./dataset/{lang}/test.json', tokenizer)
    test_dataloader = DataLoader(test_set, batch_size=batch_size)
    test_loss = evaluate(model, test_dataloader, criterion, device, mode='test')
    print(f"Test Loss: {test_loss}")

def main():
    model_name = 'huggingface/CodeBERTa-small-v1'
    batch_size = 32
    lang = 'python'
    train = False
    test = True
    print(f'--model: {model_name}, --lang: {lang}, --train: {train}, --test {test}, --batch_size: {batch_size}')
    train_on_single_lang(model_name, lang, batch_size, train, test, None)

if __name__ == "__main__":
    main()
