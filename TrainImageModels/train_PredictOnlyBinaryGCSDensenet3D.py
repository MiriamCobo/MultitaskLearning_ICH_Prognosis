import logging
import os
import sys
import shutil
import matplotlib.pyplot as plt
import torch
import pandas as pd
from sklearn.model_selection import train_test_split
from torch.optim.lr_scheduler import LambdaLR
from sklearn.metrics import roc_auc_score, accuracy_score, precision_score, recall_score, f1_score, balanced_accuracy_score, confusion_matrix
import torch
import torch.nn as nn
import functools
import operator
from sklearn.model_selection import StratifiedKFold
from torchsummary import summary

from IM_outputPrognosisGCS_Pytorch import BinaryGCS_Model

# from torch.utils.tensorboard import SummaryWriter
import numpy as np
from imblearn.over_sampling import RandomOverSampler
from sklearn.metrics import classification_report, cohen_kappa_score
from sklearn.preprocessing import MinMaxScaler, OrdinalEncoder, OneHotEncoder
import math
import monai
from monai.apps import download_and_extract
from monai.config import print_config
from monai.data import DataLoader, ImageDataset, Dataset, CacheDataset
from monai.transforms import (
    LoadImaged,
    EnsureChannelFirstd,
    DivisiblePadd,
    Flipd,
    ThresholdIntensityd,
    NormalizeIntensityd,
    ScaleIntensityRanged,
    ScaleIntensityd,
    SpatialPadd,
    Compose,
    Rotate90d,
    RandRotate90,
    Resized,
    ScaleIntensity,
    Transposed,
    AsDiscrete,
    Activations,
    Transpose,
    RandRotate90d,
    RandGaussianNoised,
    Rand3DElasticd, 
    RandRotated,
    RandAffined,
    Zoomd
)
from evaluate_thresholds import EvaluateThresholds
import torch.nn.functional as F

import json

def load_config(config_path):
    with open(config_path, 'r') as config_file:
        config = json.load(config_file)
    return config

# Load the config file
config = load_config('/home/ubuntu/tenerife/miriam/MultitaskLearning_ICH_Prognosis/config.json')

# Access the variables from the config dictionary
path_to_save_model_dir = config["path_to_save_model_dir"]
path_to_save_results = config["path_to_save_results"]
name_file = config["name_file"]

np.set_printoptions(precision=3)

# path_to_save_model_dir = "/home/ubuntu/tenerife/data/ZZ_ICH_PrognosisMICCAI/Models/BinaryGCS"
# path_to_save_results = '/home/ubuntu/tenerife/data/ZZ_ICH_PrognosisMICCAI/Results/BinaryGCS'
# name_file = "BinaryGCS"

sys.stdout=open(os.path.join(path_to_save_results, "run_out_"+name_file+"_10fold.txt"),'w')
# save prints in a txt file
original_stdout = sys.stdout

pin_memory = torch.cuda.is_available()
str_cuda="cuda:1"
device = torch.device(str_cuda if torch.cuda.is_available() else "cpu")
logging.basicConfig(stream=sys.stdout, level=logging.INFO)
print_config()

def print_specific_layer_weights(model, layer_name):
    # print("Name layers in model:")
    for name, param in model.named_parameters():
        # print(name)
        if name == layer_name:
            print(f"Weights of {name}:", param.data)

image_shape=301
depth=40

# Set data directory
directory = "/home/ubuntu/tenerife/data/ICH_nii_StrippedSkull"
# read images and corresponding labels from directory
goodPrognosis_images = sorted(os.listdir(os.path.join(directory, "GOOD_PROGNOSIS")))
print(f"Good prognosis images(0): {len(goodPrognosis_images)}")
PoorPrognosis_images = sorted(os.listdir(os.path.join(directory, "POOR_PROGNOSIS")))
print(f"Poor prognosis images (1): {len(PoorPrognosis_images)}")

# read CLINICAL DATA
clinical_data = pd.read_csv('/home/ubuntu/tenerife/data/ICH_tabular_data/CLINICAL_DATA_ICH.csv', delimiter=',')
clinical_data = clinical_data.rename(columns={'PatientID': 'Patient'})
# drop row where Patient is 213
clinical_data_filtered = clinical_data[~clinical_data['Patient'].isin([213])]
clinical_data_filtered = clinical_data_filtered.reset_index(drop=True)

images_all=[]
labels_all=[]
# loop over Patient in clinical_data
for i in range(len(clinical_data)):
    # get patient ID
    patientID = clinical_data['Patient'][i]
    # get label
    label = clinical_data['Label (poor_prognosis)'][i]
    # read corresponding image
    if label==0:
        # check that image exists
        if os.path.isfile(os.path.join(directory, "GOOD_PROGNOSIS", str(patientID) + "_brain.nii.gz")):
            images_all.append(os.path.join(directory, "GOOD_PROGNOSIS", str(patientID) + "_brain.nii.gz"))
            labels_all.append(label)
    elif label==1:
        # check that image exists
        if os.path.isfile(os.path.join(directory, "POOR_PROGNOSIS", str(patientID) + "_brain.nii.gz")):
            images_all.append(os.path.join(directory, "POOR_PROGNOSIS", str(patientID) + "_brain.nii.gz"))
            labels_all.append(label)
    else:
        print("ERROR: for Patient", patientID, "label not found")
print("Number of images:", len(images_all))
print("Number of labels:", len(labels_all))
images_all=np.array(images_all)
labels_all=np.array(labels_all)

seed = 1
torch.manual_seed(seed)
torch.cuda.manual_seed(seed)

# create 5 fold cross validation

skf = StratifiedKFold(n_splits=10, shuffle=True, random_state=1)
print("=" * 80)
# save best metrics for each fold
fold_metrics_df = pd.DataFrame(columns=['Fold', 'AUC_GCS', 'Balanced_accuracy_GCS', 'Accuracy_GCS', 'Specificity_GCS', 'NPV_GCS', 'Precision_GCS', 'Recall_GCS', 'F1-score_GCS',
                                         'AUC_Age', 'Balanced_accuracy_Age', 'Accuracy_Age', 'Specificity_Age', 'NPV_Age', 'Precision_Age', 'Recall_Age', 'F1-score_Age'])
# save metrics for best threshold regarding recall for each fold
fold_metrics_recall_df = pd.DataFrame(columns=['Fold', 'AUC', 'Threshold', 'Balanced_accuracy', 'Accuracy', 'Specificity', 'NPV', 'Precision', 'Recall', 'F1-score'])
# save metrics for best threshold regarding f1-score for each fold
fold_metrics_f1_df = pd.DataFrame(columns=['Fold', 'AUC', 'Threshold', 'Balanced_accuracy', 'Accuracy', 'Specificity', 'NPV', 'Precision', 'Recall', 'F1-score'])
# save test labels and predictions for each fold
test_labels_df = pd.DataFrame(columns=['Fold', 'True_labels_GCS', 'Probabilities_labels_test_GCS', 
                                       'Predicted_labels_test_th0.5_GCS'])

# Iterate over the folds
for fold, (train_index, test_index) in enumerate(skf.split(images_all, labels_all)):
    print(f"Fold {fold + 1}:")

    # Split the data into train and test sets
    images_train, images_test = images_all[train_index], images_all[test_index]
    labels_train, labels_test = labels_all[train_index], labels_all[test_index]

    # Further split the training set into train and validation sets
    images_train, images_val, labels_train, labels_val = train_test_split(
        images_train, labels_train, test_size=0.1, random_state=1, stratify=labels_train
    )

    # convert back to list
    images_train = images_train.tolist()
    images_val = images_val.tolist()
    images_test = images_test.tolist()
    labels_train = labels_train.tolist()
    labels_val = labels_val.tolist()
    labels_test = labels_test.tolist()

    # Print information for each fold
    print("Training set:", len(images_train), "images,", labels_train.count(0), "good prognosis,", labels_train.count(1), "poor prognosis")
    print("Validation set:", len(images_val), "images,", labels_val.count(0), "good prognosis,", labels_val.count(1), "poor prognosis")
    print("Test set:", len(images_test), "images,", labels_test.count(0), "good prognosis,", labels_test.count(1), "poor prognosis")
    print("=" * 80)

    # Count the occurrences of each class in the training set
    class_counts = {0: labels_train.count(0), 1: labels_train.count(1)}

    # Determine the target count for balancing
    target_count = max(class_counts.values())

    # Perform random oversampling on the minority class (class 1) ### TODO the oversampling technique could be improved
    ros = RandomOverSampler(sampling_strategy={0: target_count, 1: target_count}, random_state=2)
    images_res, labels_res = ros.fit_resample(np.array(images_train).reshape(-1, 1), np.array(labels_train).reshape(-1, 1))
    labels_res = labels_res.tolist()
    images_res = [item for sublist in images_res for item in sublist]
    print("Training set after resampling:", len(images_res), "images,", labels_res.count(0), "good prognosis,", labels_res.count(1), "poor prognosis")
    
    # clinical data
    df = clinical_data_filtered
    # rename patient column
    df = df.rename(columns={'Patient': 'PatientID'})
    print("Shape clinical data dataframe:", df.shape)

    # loop over images_train, images_val and images_test to get the corresponding clinical data
    X_train = pd.DataFrame()
    X_test = pd.DataFrame()
    X_val = pd.DataFrame()
    for i in images_res:
        patientID = int(i.split('/')[-1].split('_brain.')[0])
        # get all columns in df for this patient
        X_train = pd.concat([X_train, df[df['PatientID']==patientID]])
    for i in images_test:
        patientID = int(i.split('/')[-1].split('_brain.')[0])
        # get all columns in df for this patient
        X_test = pd.concat([X_test, df[df['PatientID']==patientID]])
    for i in images_val:
        patientID = int(i.split('/')[-1].split('_brain.')[0])
        # get all columns in df for this patient
        X_val = pd.concat([X_val, df[df['PatientID']==patientID]])

    # remove PatientID from X_train, X_test and X_val
    X_train = X_train.drop(columns=['PatientID', 'Label (poor_prognosis)'])
    X_test = X_test.drop(columns=['PatientID', 'Label (poor_prognosis)'])
    X_val = X_val.drop(columns=['PatientID', 'Label (poor_prognosis)'])
    
    only_GCS=True
    only_Age=True

    if only_GCS==True:
        gcs_train = X_train[['GCS']]
        gcs_test = X_test[['GCS']]
        gcs_val = X_val[['GCS']]
        ordinal_categorical_var=['GCS']
        # binary encode, if gCS <=8 then 0, if GCS >8 then 1
        gcs_train['GCS'] = gcs_train['GCS'].apply(lambda x: 1 if x<=8 else 0)
        gcs_test['GCS'] = gcs_test['GCS'].apply(lambda x: 1 if x<=8 else 0)
        gcs_val['GCS'] = gcs_val['GCS'].apply(lambda x: 1 if x<=8 else 0)
        print("Number of ordinal categorical variables:", len(ordinal_categorical_var))
        print("Distribution of values of GCS after binaryzation")
        print("Train:", gcs_train['GCS'].value_counts())
        print("Val:", gcs_val['GCS'].value_counts())
        print("Test:", gcs_test['GCS'].value_counts())
        
        # convert to tensor
        gcs_train_tensor = gcs_train['GCS'].values.tolist()
        gcs_val_tensor = gcs_val['GCS'].values.tolist()
        gcs_test_tensor = gcs_test['GCS'].values.tolist()

        # for the weights:
        num_positive_gcs = sum(label == 1 for label in gcs_train_tensor)
        num_negative_gcs = sum(label == 0 for label in gcs_train_tensor)
        # Calculate the weight for the positive class
        pos_weight_gcs = torch.tensor([num_negative_gcs / num_positive_gcs], dtype=torch.float32)
        print("Positive weight GCS:", pos_weight_gcs)
    
    if only_Age==True:
        age_train = X_train[['Age']]
        age_test = X_test[['Age']]
        age_val = X_val[['Age']]
        ordinal_regression_var=['Age']
        # binary encode, if age <=65 then 0, if age >65 then 1
        age_train['Age'] = age_train['Age'].apply(lambda x: 1 if x>=80 else 0)
        age_test['Age'] = age_test['Age'].apply(lambda x: 1 if x>=80 else 0)
        age_val['Age'] = age_val['Age'].apply(lambda x: 1 if x>=80 else 0)
        print("Number of ordinal categorical variables:", len(ordinal_regression_var))
        print("Distribution of values of Age after binaryzation")
        print("Train:", age_train['Age'].value_counts())
        print("Val:", age_val['Age'].value_counts())
        print("Test:", age_test['Age'].value_counts())
        
        # convert to tensor
        age_train_tensor = age_train['Age'].values.tolist()
        age_val_tensor = age_val['Age'].values.tolist()
        age_test_tensor = age_test['Age'].values.tolist()
    
    train_files = [{"image": image_name, "label": label_name, "gcs":gcs_name, "age": age_name} for image_name, label_name, gcs_name, age_name in zip(images_res, labels_res, gcs_train_tensor, age_train_tensor)]
    val_files = [{"image": image_name, "label": label_name, "gcs":gcs_name, "age": age_name} for image_name, label_name, gcs_name, age_name in zip(images_val, labels_val, gcs_val_tensor, age_val_tensor)]
    test_files = [{"image": image_name, "label": label_name, "gcs":gcs_name, "age": age_name} for image_name, label_name, gcs_name, age_name in zip(images_test, labels_test, gcs_test_tensor, age_test_tensor)]
    
    # https://github.com/Project-MONAI/tutorials/blob/main/modules/load_medical_images.ipynb

    # Define transforms
    train_transforms = Compose(
        [LoadImaged(keys="image", image_only=True, ensure_channel_first=True, reader="ITKReader"),
        Rotate90d(keys="image", k=3), 
        Flipd(keys="image", spatial_axis=1),
        NormalizeIntensityd(keys="image", subtrahend=15, divisor=85),
        ThresholdIntensityd(keys="image", threshold=0, above=True, cval=0.0),
        ThresholdIntensityd(keys="image", threshold=1, above=False, cval=0.0),
        SpatialPadd(keys="image", spatial_size=[-1, -1, depth], mode=('constant'), method= ("symmetric")),
        Resized(keys="image", spatial_size=[image_shape, image_shape, depth]),
        RandRotated(keys="image", prob=0.5, range_z=np.pi/36, range_y=np.pi/36, range_x=np.pi/36), # randzoom, rand3delastic could also be used
        # RandAffined(keys="image", scale_range=(0.9, 1), rotate_range=(np.pi/18, np.pi/18), prob=0.5),
        Zoomd(keys="image", zoom=1.1, prob=0.5),
        # Rand3DElasticd(keys="image", sigma_range=(2, 3), magnitude_range=(100, 200), prob=0.1),
        RandGaussianNoised(keys="image", prob=0.5, mean=0.0, std=0.01),
        Transposed(keys="image", indices=[0, 3, 1, 2])
        ])

    val_transforms = Compose(
        [LoadImaged(keys="image", image_only=True, ensure_channel_first=True, reader="ITKReader"), 
        Rotate90d(keys="image", k=3), 
        Flipd(keys="image", spatial_axis=1),
        NormalizeIntensityd(keys="image", subtrahend=15, divisor=85),
        ThresholdIntensityd(keys="image", threshold=0, above=True, cval=0.0),
        ThresholdIntensityd(keys="image", threshold=1, above=False, cval=0.0),
        SpatialPadd(keys="image", spatial_size=[-1, -1, depth], mode=('constant'), method= ("symmetric")),
        Resized(keys="image", spatial_size=[image_shape, image_shape, depth]),
        Transposed(keys="image", indices=[0, 3, 1, 2])
        ])

    # Define nifti dataset, data loader
    check_ds = Dataset(data=train_files, transform=train_transforms)
    check_loader = DataLoader(check_ds, batch_size=1)
    try:
        result = monai.utils.misc.first(check_loader)
        print(type(result['image']), result['image'].shape, result['label'])
    except RuntimeError as e:
        print("Error occurred when applying transform:", e)
    # create a training data loader
    # train_ds = CacheDataset(data=train_files, transform=train_transforms, cache_rate=1.0, num_workers=4)
    train_ds = Dataset(data=train_files, transform=train_transforms)
    train_loader = DataLoader(train_ds, batch_size=8, shuffle=True, num_workers=2, pin_memory=pin_memory)

    # you can use Dataset or CacheDataset, according to 
    # this https://github.com/Project-MONAI/tutorials/blob/main/3d_segmentation/spleen_segmentation_3d.ipynb
    # the later is faster

    # create a validation data loader
    # val_ds = CacheDataset(data=val_files, transform=val_transforms, cache_rate=1.0, num_workers=4)
    val_ds = Dataset(data=val_files, transform=val_transforms)
    val_loader = DataLoader(val_ds, batch_size=8, num_workers=2, pin_memory=pin_memory)
    path_to_save_model=os.path.join(path_to_save_model_dir,
                                                "DenseNet_"+name_file+"_fold"+str(fold)+".pth")
    if not os.path.isfile(path_to_save_model):
        model = monai.networks.nets.DenseNet121(spatial_dims=3, in_channels=1, out_channels=1, dropout_prob=0.2)
        # print the name of the layers in the model
        # print("Name layers in model:")
        # for name, param in model.named_parameters():
        #     print(name)
        
        total_params = sum(
            param.numel() for param in model.parameters()
        )
        print("Total params model", total_params)

        # model = model.cuda()

        threshold = 0.5
        ###
        # if torch.cuda.device_count() > 1:
        #     print("Let's use", torch.cuda.device_count(), "GPUs")
        #     model= torch.nn.DataParallel(model)

        model.to(device)
        # print("Model's summary:")
        # print(summary(model, (1, image_shape, image_shape, depth)))

        # loss_function = torch.nn.CrossEntropyLoss() # this is for 2 out_channels output
        ordinal_loss_function_GCS = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weight_gcs).to(device)

        optimizer = torch.optim.AdamW(model.parameters(), lr=0.001, weight_decay=0.0001, amsgrad=True)
        # optimizer = torch.optim.SGD(model.parameters(), lr=1e-3, momentum=0.9)
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'min', patience=5, factor=0.98, verbose=False)
        #### understand well all the training and revise
        #### create custom model to append at the end tabular data and image features
        val_interval = 1
        best_gcs_metric = -1
        best_gcs_metric_epoch = -1
        epoch_gcs_accuracy_values = []
        epoch_gcs_auc_values = []
        val_gcs_auc_values = []
        val_gcs_accuracy_values = []
        epoch_loss_values = []

        max_epochs = 100

        # writer = SummaryWriter()

        # early stopping
        patience = 20
        epochs_no_improve = 0
        early_stop = False
        accumulation_steps = 3

        for epoch in range(max_epochs):
            print("-" * 10)
            print(f"epoch {epoch + 1}/{max_epochs}")
            print('Epoch-{0} lr: {1}'.format(epoch + 1, optimizer.param_groups[0]['lr']))
            model.train()
            epoch_loss = 0
            step = 0
            all_gcs_predictions = []  # To store predicted values for GCS
            all_gcs_probabilities = []  # To store corresponding probabilities for GCS
            all_gcs_labels = []  # To store ground truth for GCS

            predictions_gcs_train=[]
            labels_gcs_train=[]
            probabilities_gcs_train=[]

            optimizer.zero_grad()
            i=0
            for batch_data in train_loader:
                i+=1
                step += 1
                inputs, labels, labels_gcs, labels_age = batch_data["image"].to(device), batch_data["label"], batch_data["gcs"].to(device), batch_data["age"]
                # optimizer.zero_grad()
                # print(inputs.shape)
                ordinal_output_gcs = model(inputs)
                ### sequeeze to get the right output shape
                # print("Predicted probabilities binary output:", binary_output)
                ordinal_output_gcs = ordinal_output_gcs.squeeze()
                # print("Labels GCS:", labels_gcs.squeeze().long())
                ordinal_loss_gcs = ordinal_loss_function_GCS(ordinal_output_gcs, labels_gcs.float())

                loss = ordinal_loss_gcs / accumulation_steps  # Normalize loss for accumulation
                loss.backward()  # Accumulate gradients

                if (i + 1) % accumulation_steps == 0:
                    optimizer.step()  # Update model parameters
                    # model.binary_head[-1].weight.data.clamp_(min=0)
                    # model.binary_head[-1].bias.data.clamp_(min=0)
                    # model.ordinal_head[-1].weight.data.clamp_(min=0)
                    # model.ordinal_head[-1].bias.data.clamp_(min=0)
                    scheduler.step(loss)  # Scheduler step (if scheduler requires loss)
                    optimizer.zero_grad()  # Reset gradients
                    # print("Already clamped weights and biases")
                    # print("Model binary head weights after clamping:", model.binary_head[-1].weight.data.min())
                    # print("Model binary head biases after clamping:", model.binary_head[-1].bias.data.min())
                    # print("Model ordinal head weights after clamping:", model.ordinal_head[-1].weight.data.min())
                    # print("Model ordinal head biases after clamping:", model.ordinal_head[-1].bias.data.min())

                epoch_loss += loss.item() * accumulation_steps  # Correct loss scaling
                epoch_len = len(train_ds) // train_loader.batch_size
                # for GCS
                labels_gcs_train.extend(labels_gcs.cpu().numpy())
                ordinal_probabilities = nn.Sigmoid()(ordinal_output_gcs)
                probabilities_gcs_train.extend(ordinal_probabilities.detach().cpu().numpy())
                # print("Predicted probabilities ordinal output GCS:", ordinal_probabilities)
                # Predicted classes
                predicted_ordinal_classes = (ordinal_probabilities >= threshold).float()
                predictions_gcs_train.extend(predicted_ordinal_classes.detach().cpu().numpy())
                # print("Predicted classes ordinal output GCS:", predicted_ordinal_classes)
                # print(labels.cpu().numpy(), train_predictions.detach().cpu().numpy())

            epoch_loss /= step
            epoch_loss_values.append(epoch_loss)
            gcs_accuracy_train = accuracy_score(labels_gcs_train, predictions_gcs_train)
            gcs_auc_train = roc_auc_score(labels_gcs_train, probabilities_gcs_train)
            print(f"Train epoch {epoch + 1} GCS accuracy: {gcs_accuracy_train:.3f}, GCS AUC: {gcs_auc_train:.3f}")
            # print(f"Train epoch {epoch + 1} GCS accuracy: {gcs_accuracy_train:.3f}, weighted (quadratic) kappa: {weighted_kappa_train:.3f}")
            epoch_gcs_accuracy_values.append(gcs_accuracy_train)
            epoch_gcs_auc_values.append(gcs_auc_train)
            
            if (epoch + 1) % val_interval == 0:
                model.eval()

                num_correct = 0.0
                metric_count = 0
                for val_data in val_loader:
                    val_images, val_labels, val_labels_gcs, val_labels_age = val_data["image"].to(device), val_data["label"], val_data["gcs"].to(device), val_data["age"]
                    with torch.no_grad():
                        ordinal_val_outputs_GCS = model(val_images)
                        # for GCS
                        ordinal_val_outputs_GCS = ordinal_val_outputs_GCS.squeeze()
                        ordinal_val_probabilities = nn.Sigmoid()(ordinal_val_outputs_GCS)
                        predicted_val_ordinal_classes = (ordinal_val_probabilities >= threshold).float()
                        all_gcs_predictions.extend(predicted_val_ordinal_classes.cpu().numpy())
                        all_gcs_labels.extend(val_labels_gcs.cpu().numpy())
                        all_gcs_probabilities.extend(ordinal_val_probabilities.cpu().numpy())

                # for GCS
                all_gcs_predictions = np.array(all_gcs_predictions).astype(int)
                all_gcs_probabilities = np.array(all_gcs_probabilities)
                all_gcs_labels = np.array(all_gcs_labels)
                unique_gcs, counts_gcs = np.unique(all_gcs_predictions, return_counts=True)
                print("Predictions count GCS:", dict(zip(unique_gcs, counts_gcs)))
                print("Probabilities in validation GCS:", all_gcs_probabilities)
                # Calculate metrics
                roc_auc_gcs = roc_auc_score(all_gcs_labels, all_gcs_probabilities,average='weighted')
                accuracy_gcs = balanced_accuracy_score(all_gcs_labels, all_gcs_predictions)
                precision_gcs = precision_score(all_gcs_labels, all_gcs_predictions, average='weighted')
                recall_gcs = recall_score(all_gcs_labels, all_gcs_predictions, average='weighted')
                f1_gcs = f1_score(all_gcs_labels, all_gcs_predictions, average='weighted')
                val_gcs_auc_values.append(roc_auc_gcs)
                val_gcs_accuracy_values.append(accuracy_gcs)

                # to perform early-stopping we select the best metric in prognosis
                if accuracy_gcs > best_gcs_metric:
                    best_gcs_metric = accuracy_gcs
                    best_gcs_metric_epoch = epoch + 1
                    torch.save(model.state_dict(), path_to_save_model)
                    print("saved new best metric model")
                    epochs_no_improve = 0
                else:
                    epochs_no_improve += 1
                    # Check early stopping condition
                    if epochs_no_improve == patience:
                        print('Early stopping!')
                        early_stop = True
                        break
                # Print metrics for each epoch
                print(f"Epoch {epoch + 1}/{max_epochs} - AUC: {roc_auc_gcs:.3f}, Accuracy: {accuracy_gcs:.3f}, Precision: {precision_gcs:.3f}, Recall: {recall_gcs:.3f}, F1-score: {f1_gcs:.3f}")

                print(
                        f" current GCS AUC: {roc_auc_gcs:.3f}"
                        f" current GCS accuracy: {accuracy_gcs:.3f}"
                        f" best GCS accuracy: {best_gcs_metric:.3f}"
                        f" at epoch: {best_gcs_metric_epoch}"
                )
                # for GCS
                print(f"Epoch {epoch + 1}/{max_epochs} - GCS AUC: {roc_auc_gcs:.3f}, GCS Accuracy: {accuracy_gcs:.3f}, GCS Precision: {precision_gcs:.3f}, GCS Recall: {recall_gcs:.3f}, GCS F1-score: {f1_gcs:.3f}")

        print(f"Training completed, best_metric in prognosis: {best_gcs_metric:.3f} at epoch: {best_gcs_metric_epoch}")
        print("-" * 50)
        # writer.close()
        plt.figure("train", (12, 6))
        plt.subplot(1, 3, 1)
        plt.title("Epoch Average Loss")
        x = [i + 1 for i in range(len(epoch_loss_values))]
        y = epoch_loss_values
        plt.xlabel("epoch")
        plt.plot(x, y)
        plt.subplot(1, 3, 2)
        plt.title("AUC")
        x = [i + 1 for i in range(len(epoch_gcs_auc_values))]
        y = epoch_gcs_auc_values
        plt.plot(x, y)
        x = [val_interval * (i + 1) for i in range(len(val_gcs_auc_values))]
        y = val_gcs_auc_values
        plt.plot(x, y)
        plt.xlabel("epoch")
        plt.legend(['train_GCS','val_GCS'], loc='best')
        plt.subplot(1, 3, 3)
        plt.title("Balanced ccuracy")
        x = [i + 1 for i in range(len(epoch_gcs_accuracy_values))]
        y = epoch_gcs_accuracy_values
        plt.plot(x, y)
        x = [val_interval * (i + 1) for i in range(len(val_gcs_accuracy_values))]
        y = val_gcs_accuracy_values
        plt.plot(x, y)
        plt.xlabel("epoch")
        plt.legend(['train_GCS', 'val_GCS'], loc='best')
        plt.savefig(os.path.join(path_to_save_results, "TrainAndVal_"+name_file+"_fold"+str(fold)+".png"))
        plt.close()

    print("Inference in test")
    threshold = 0.5

    test_ds = Dataset(data=test_files, transform=val_transforms)
    test_loader = DataLoader(test_ds, batch_size=8)

    model = monai.networks.nets.DenseNet121(spatial_dims=3, in_channels=1, out_channels=1, dropout_prob=0.2)
    model.to(device)
    model.load_state_dict(torch.load(path_to_save_model))
    model.eval()
    # for GCS
    predicted_labels_gcs_test = []
    all_probabilities_gcs_test = []
    labels_gcs_test_tensor = []
    with torch.no_grad():
        for test_data in test_loader:
            test_images, test_labels, test_labels_gcs, test_labels_age = test_data["image"].to(device), test_data["label"], test_data["gcs"].to(device), test_data["age"]
            ordinal_test_outputs = model(test_images)
            #  for GCS
            ordinal_test_outputs = ordinal_test_outputs.squeeze()
            ordinal_probabilities_test = nn.Sigmoid()(ordinal_test_outputs)
            predicted_ordinal_classes_test = (ordinal_probabilities_test >= threshold).float()
            all_probabilities_gcs_test.extend(ordinal_probabilities_test.cpu().numpy())
            predicted_labels_gcs_test.extend(predicted_ordinal_classes_test.cpu().numpy())
            labels_gcs_test_tensor.extend(test_labels_gcs.cpu().numpy())            

    # for GCS
    all_probabilities_gcs_test = np.array(all_probabilities_gcs_test)
    labels_gcs_test_tensor = np.array(labels_gcs_test_tensor).astype(int)
    predicted_labels_gcs_test=np.array(predicted_labels_gcs_test).astype(int)
    # create a fold_array that repeates the fold number as many times as test samples
    fold_array = np.full((len(labels_gcs_test_tensor)), fold)
    combined=np.column_stack((fold_array, labels_gcs_test_tensor, all_probabilities_gcs_test, predicted_labels_gcs_test))
    test_labels_df = pd.concat([test_labels_df, pd.DataFrame(combined, columns=test_labels_df.columns)], ignore_index=True)
    # for GCS
    test_gcs_balanced_accuracy = balanced_accuracy_score(labels_gcs_test_tensor, predicted_labels_gcs_test)
    test_gcs_accuracy = accuracy_score(labels_gcs_test_tensor, predicted_labels_gcs_test)
    test_gcs_auc = roc_auc_score(labels_gcs_test_tensor, all_probabilities_gcs_test)
    test_gcs_precision = precision_score(labels_gcs_test_tensor, predicted_labels_gcs_test)
    test_gcs_recall = recall_score(labels_gcs_test_tensor, predicted_labels_gcs_test)
    test_gcs_f1 = f1_score(labels_gcs_test_tensor, predicted_labels_gcs_test)

    tn_gcs, fp_gcs, fn_gcs, tp_gcs = confusion_matrix(labels_gcs_test_tensor, predicted_labels_gcs_test, labels=[0, 1]).ravel()
    test_gcs_NPV=tn_gcs/(tn_gcs+fn_gcs)
    test_gcs_specificity=tn_gcs/(tn_gcs+fp_gcs)

    # save in fold_metrics_df
    fold_metrics_df = pd.concat([fold_metrics_df, pd.DataFrame([{'Fold': fold,
                'AUC_GCS':test_gcs_auc, 'Balanced_accuracy_GCS': test_gcs_balanced_accuracy,
                'Accuracy_GCS': test_gcs_accuracy, 'Specificity_GCS': test_gcs_specificity, 
                'NPV_GCS': test_gcs_NPV, 'Precision_GCS': test_gcs_precision, 'Recall_GCS': test_gcs_recall,
                'F1-score_GCS': test_gcs_f1}])], ignore_index=True)

    print("Probabilities test in GCS:", all_probabilities_gcs_test)
    unique_gcs, counts_gcs = np.unique(predicted_labels_gcs_test, return_counts=True)
    print("Predictions count test GCS:", dict(zip(unique_gcs, counts_gcs)))
    print(f'Test GCS Accuracy: {test_gcs_accuracy:.2%}')
    print(f'Test GCS ROC AUC: {test_gcs_auc:.4f}, Precision: {test_gcs_precision:.4f}, Recall: {test_gcs_recall:.4f}, F1: {test_gcs_f1:.4f}')

    # Save predicted labels for test set
    predicted_labels_df = pd.DataFrame({'True Labels': labels_test, 'Predicted Labels': predicted_labels_gcs_test})
    # predicted_labels_df.to_csv('predicted_labels.csv', index=False)

    # plot summary report
    print(classification_report(labels_gcs_test_tensor, predicted_labels_gcs_test, target_names=['Good prognosis', 'Poor prognosis']))

    evaluator = EvaluateThresholds(all_probabilities_gcs_test, labels_gcs_test_tensor,
             path_to_save_auc_plot=os.path.join(path_to_save_results,"AUC_plots",
                                                 "AUC_testplot_"+name_file+"_fold"+str(fold)+".png"), fold=fold)
    auc_score = evaluator.plot_roc_curve()
    print("AUC score calculated by evaluator:", auc_score)
    best_threshold_recall, best_metrics_recall = evaluator.evaluate_metrics_recall()
    print("Best threshold based on F1-score:", best_threshold_recall, "Best metrics based on recall:", best_metrics_recall)
    fold_metrics_recall_df = pd.concat([fold_metrics_recall_df, pd.DataFrame([best_metrics_recall])], ignore_index=True)
    best_threshold_f1, best_metrics_f1 = evaluator.evaluate_metrics_f1()
    print("Best threshold based on F1-score:", best_threshold_f1, "Best metrics based on f1-score:", best_metrics_f1)
    fold_metrics_f1_df = pd.concat([fold_metrics_f1_df, pd.DataFrame([best_metrics_f1])], ignore_index=True)

    try:
        test_auc_boots=bootstrapping(y_true=labels_gcs_test_tensor, y_pred=all_probabilities_gcs_test, y_pred_threshold=predicted_labels_gcs_test, 
                    path_to_save_metrics='/home/ubuntu/tenerife/data/ICH_results/tabularData_model', 
                    metrics = 'AUC', confidence = 0.95, n_bootstraps = 1000)
        print(test_auc_boots)

        all_metrics_boots=bootstrapping(y_true=labels_gcs_test_tensor, y_pred=all_probabilities_gcs_test, y_pred_threshold=predicted_labels_gcs_test, 
                    path_to_save_metrics='/home/ubuntu/tenerife/data/ICH_results/tabularData_model', 
                    metrics = 'METRICS', confidence = 0.95, n_bootstraps = 1000)
        print(all_metrics_boots)
    except Exception as e:
        print(e)

    # save fold_metrics_df
    fold_metrics_df.to_csv(os.path.join(path_to_save_results, "fold_metrics_df_"+name_file+".csv"), index=False)
    test_labels_df.to_csv(os.path.join(path_to_save_results, "test_labels_df_"+name_file+".csv"), index=False)
    fold_metrics_recall_df.to_csv(os.path.join(path_to_save_results, "fold_metrics_recall_"+name_file+".csv"), index=False)
    fold_metrics_f1_df.to_csv(os.path.join(path_to_save_results, "fold_metrics_f1_"+name_file+".csv"), index=False)

    print("=" * 80)
    

print("=" * 80)
# for GCS
print("=" * 80)
mean_auc_gcs = fold_metrics_df['AUC_GCS'].mean()
std_auc_gcs = fold_metrics_df['AUC_GCS'].std()
mean_accuracy_gcs = fold_metrics_df['Accuracy_GCS'].mean()
std_accuracy_gcs = fold_metrics_df['Accuracy_GCS'].std()
mean_balanced_accuracy_gcs = fold_metrics_df['Balanced_accuracy_GCS'].mean()
std_balanced_accuracy_gcs = fold_metrics_df['Balanced_accuracy_GCS'].std()
mean_specificity_gcs = fold_metrics_df['Specificity_GCS'].mean()
std_specificity_gcs = fold_metrics_df['Specificity_GCS'].std()
mean_NPV_gcs = fold_metrics_df['NPV_GCS'].mean()
std_NPV_gcs = fold_metrics_df['NPV_GCS'].std()
mean_precision_gcs = fold_metrics_df['Precision_GCS'].mean()
std_precision_gcs = fold_metrics_df['Precision_GCS'].std()
mean_recall_gcs = fold_metrics_df['Recall_GCS'].mean()
std_recall_gcs = fold_metrics_df['Recall_GCS'].std()
mean_f1_gcs = fold_metrics_df['F1-score_GCS'].mean()
std_f1_gcs = fold_metrics_df['F1-score_GCS'].std()
# print metrics
print("Mean AUC GCS:", mean_auc_gcs, "Std AUC GCS:", std_auc_gcs)
print("Mean Accuracy GCS:", mean_accuracy_gcs, "Std Accuracy GCS:", std_accuracy_gcs)
print("Mean Balanced accuracy GCS:", mean_balanced_accuracy_gcs, "Std Balanced accuracy GCS:", std_balanced_accuracy_gcs)
print("Mean Specificity GCS:", mean_specificity_gcs, "Std Specificity GCS:", std_specificity_gcs)
print("Mean NPV GCS:", mean_NPV_gcs, "Std NPV GCS:", std_NPV_gcs)
print("Mean Precision GCS:", mean_precision_gcs, "Std Precision GCS:", std_precision_gcs)
print("Mean Recall GCS:", mean_recall_gcs, "Std Recall GCS:", std_recall_gcs)
print("Mean F1-score GCS:", mean_f1_gcs, "Std F1-score GCS:", std_f1_gcs)
print("=" * 80)
print("=" * 80)
# save fold_metrics_df
fold_metrics_df.to_csv(os.path.join(path_to_save_results, "fold_metrics_df_"+name_file+".csv"), index=False)
test_labels_df.to_csv(os.path.join(path_to_save_results, "test_labels_df_"+name_file+".csv"), index=False)
fold_metrics_recall_df.to_csv(os.path.join(path_to_save_results, "fold_metrics_recall_"+name_file+".csv"), index=False)
fold_metrics_f1_df.to_csv(os.path.join(path_to_save_results, "fold_metrics_f1_"+name_file+".csv"), index=False)

sys.stdout.close()