# =============================================================================
# IMPORTS
# =============================================================================
import argparse
import os

import numpy as np
import torch

import espaloma as esp

def run(args):
    # define data
    g = esp.Graph("CN1C=NC2=C1C(=O)N(C(=O)N2C)C")
    data = esp.data.dataset.GraphDataset([g])

    # get force field
    forcefield = esp.graphs.legacy_force_field.LegacyForceField(
        args.forcefield
    )


    # apply to dataset
    data = data.apply(forcefield.parametrize, in_place=True)
    data = data.apply(forcefield.typing, in_place=True)

    # apply simulation
    # make simulation
    from espaloma.data.md import MoleculeVacuumSimulation
    simulation = MoleculeVacuumSimulation(
        n_samples=500, n_steps_per_sample=100,
    )

    data = data.apply(simulation.run, in_place=True)

    def type_bonds_and_angles(g):

        legacy_typing = g.nodes['n1'].data['legacy_typing']
        legacy_typing_one_hot = torch.zeros(legacy_typing.shape[0], 117).scatter(
                1, legacy_typing[:, None], 1.0,
        )

        bond_type_to_idx = {}
        angle_type_to_idx = {}

        _idx = 0
        n2_typing = torch.zeros(g.heterograph.number_of_nodes('n2'), dtype=torch.int32).long()
        for count, idx in enumerate(g.nodes['n2'].data['idxs']):
            idx = tuple((legacy_typing[idx[0]].item(), legacy_typing[idx[1]].item()))
            if idx in bond_type_to_idx:
                n2_typing[count] = bond_type_to_idx[idx]
            elif idx[::-1] in bond_type_to_idx:
                n2_typing[count] = bond_type_to_idx[idx[::-1]]
            else:
                bond_type_to_idx[idx] = _idx
                n2_typing[count] = _idx
                _idx += 1

        _idx = 0
        n3_typing = torch.zeros(g.heterograph.number_of_nodes('n3'), dtype=torch.int32).long()
        for count, idx in enumerate(g.nodes['n3'].data['idxs']):
            idx = tuple((legacy_typing[idx[0]].item(), legacy_typing[idx[1]].item(), legacy_typing[idx[2]].item()))
            if idx in angle_type_to_idx:
                n3_typing[count] = angle_type_to_idx[idx]
            elif idx[::-1] in angle_type_to_idx:
                n3_typing[count] = angle_type_to_idx[idx[::-1]]
            else:
                angle_type_to_idx[idx] = _idx
                n3_typing[count] = _idx
                _idx += 1

        g.nodes['n2'].data['legacy_typing'] = n2_typing
        g.nodes['n3'].data['legacy_typing'] = n3_typing

        return g

    type_bonds_and_angles(g)
    data = data.apply(type_bonds_and_angles, in_place=True)

    # batch
    ds = data.view("graph", batch_size=1)

    g = next(iter(ds))

    if args.layer != "Free":
        # layer
        layer = esp.nn.layers.dgl_legacy.gn(args.layer)

        # representation
        representation = esp.nn.Sequential(layer, config=args.config)

        # get the last bit of units
        units = [int(x) for x in args.config if isinstance(x, int) or isinstance(x, str) and x.isdigit()][-1]

        janossy_config = []
        for x in args.janossy_config:
            if isinstance(x, int):
                janossy_config.append(int(x))

            elif x.isdigit():
                janossy_config.append(int(x))

            else:
                janossy_config.append(x)

        readout = esp.nn.readout.janossy.JanossyPooling(
            in_features=units, config=janossy_config,
            out_features={
                2: {'nn_typing': 50},
                3: {'nn_typing': 50},
            },
        )

        net = torch.nn.Sequential(
                representation,
                readout,
        )

    if args.layer == "Free":
        representation = esp.nn.baselines.FreeParameterBaseline(next(iter(ds)))
        net = torch.nn.Sequential(
                representation,
                readout,
        )


    from espaloma.metrics import GraphMetric

    class BondTypingCrossEntropy(GraphMetric):
        def __init__(self):
            super(BondTypingCrossEntropy, self).__init__(
                base_metric=torch.nn.CrossEntropyLoss(),
                between=["nn_typing", "legacy_typing"],
                level="n2",
            )

            self.__name__ = "BondTypingCrossEntropy"


    class AngleTypingCrossEntropy(GraphMetric):
        def __init__(self):
            super(AngleTypingCrossEntropy, self).__init__(
                base_metric=torch.nn.CrossEntropyLoss(),
                between=["nn_typing", "legacy_typing"],
                level="n3",
            )

            self.__name__ = "AngleTypingCrossEntropy"



    class BondTypingAccuracy(GraphMetric):
        def __init__(self):
            super(BondTypingAccuracy, self).__init__(
                base_metric=esp.metrics.accuracy,
                between=["nn_typing", "legacy_typing"],
                level="n2",
            )

            self.__name__ = "BondTypingAccuracy"


    class AngleTypingAccuracy(GraphMetric):
        def __init__(self):
            super(AngleTypingAccuracy, self).__init__(
                base_metric=esp.metrics.accuracy,
                between=["nn_typing", "legacy_typing"],
                level="n3",
            )

            self.__name__ = "AngleTypingAccuracy"

    metrics_tr = [BondTypingCrossEntropy(), AngleTypingCrossEntropy()]
    metrics_te = [BondTypingAccuracy(), AngleTypingAccuracy()]

    if args.opt == "Adam":
        opt = torch.optim.Adam(net.parameters(), 1e-5)

        if args.metric_train == "param":
            opt = torch.optim.Adam(net.parameters(), 1e-1)

    elif args.opt == "SGD":
        opt = torch.optim.SGD(net.parameters(), 1e-5, 1e-5)

    elif args.opt == "LBFGS":
        opt = torch.optim.LBFGS(net.parameters(), 1e-1, line_search_fn="strong_wolfe")

    elif args.opt == "SGLD":
        from pinot.samplers.sgld import SGLD
        opt = SGLD(net.parameters(), 1e-5)

    exp = esp.TrainAndTest(
        ds_tr=ds,
        ds_te=ds,
        net=net,
        metrics_tr=metrics_tr,
        metrics_te=metrics_te,
        n_epochs=args.n_epochs,
        normalize=esp.data.normalize.NotNormalize,
        record_interval=100,
        optimizer=opt,
        device=torch.device('cuda:0'),
    )

    results = exp.run()

    print(esp.app.report.markdown(results))

    os.mkdir(args.out)

    with open(args.out + "/architecture.txt", "w") as f_handle:
        f_handle.write(str(exp))

    with open(args.out + "/result_table.md", "w") as f_handle:
        f_handle.write(esp.app.report.markdown(results))

    curves = esp.app.report.curve(results)

    for spec, curve in curves.items():
        np.save(args.out + "/" + "_".join(spec) + ".npy", curve)

    import pickle
    pickle.dump(
        exp.ref_g_test,
        open(args.out + "/g.th", 'wb'),
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--first", default=-1, type=int)
    parser.add_argument("--partition", default="4:1", type=str)
    parser.add_argument("--batch_size", default=8, type=int)
    parser.add_argument("--forcefield", default="gaff-1.81", type=str)
    parser.add_argument("--layer", default="GraphConv", type=str)
    parser.add_argument("--n_classes", default=100, type=int)
    parser.add_argument(
        "--config", nargs="*", default=[32, "tanh", 32, "tanh", 32, "tanh"]
    )

    parser.add_argument(
        "--training_metrics", nargs="*", default=["TypingCrossEntropy"]
    )
    parser.add_argument(
        "--test_metrics", nargs="*", default=["TypingAccuracy"]
    )
    parser.add_argument(
        "--out", default="results", type=str
    )
    parser.add_argument("--janossy_config", nargs="*", default=[32, "leaky_relu"])

    parser.add_argument("--n_epochs", default=10, type=int)

    parser.add_argument("--opt", default="Adam", type=str)
    parser.add_argument("--metric_train", default="energy", type=str)

    args = parser.parse_args()

    run(args)
